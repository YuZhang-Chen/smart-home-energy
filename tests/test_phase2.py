import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from smart_home_energy.ac import VirtualAC, ACCommand
from smart_home_energy.ac_domain import ACConstraints, ACApproval, make_ac_plan, predict, cooling_energy
from smart_home_energy.ac_control import ACController
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import TAIPEI, TaskConstraints, Tariff, WashProgram, encode, decode, make_plan, utcnow
from smart_home_energy.multi_agent import run_household
from smart_home_energy.repository import Repository, Task
from smart_home_energy.tariffs import configure_tariff, selected_tariff, estimate_incremental_cost, household_estimate
from test_agent import FakeModel, action
from test_control import FakeTransport


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(self.directory / "control.db")
        self.hems = replace(HemsSettings.from_env(), database=self.repo.path, device_database=str(self.directory / "washer.db"))
        self.real = utcnow()
        self.clock = self.repo.clock()
        self.c = ACConstraints(self.clock.now(self.real) + timedelta(minutes=5), self.clock.now(self.real) + timedelta(hours=2))
        self.device_repo = Repository(self.directory / "ac.db")
        self.ac = VirtualAC(self.device_repo, self.clock, self.real)
        self.repo.save_device(self.ac.state)

    def proposed_ac(self):
        task = self.repo.create_ac_task(self.c, Tariff())
        p = make_ac_plan(self.c, 30, task.tariff, self.clock.clock_id)
        return self.repo.propose_ac(task.task_id, task.version, p)

    def test_legacy_database_migrates_without_replacing_history(self):
        path = self.directory / "old.db"
        connection = sqlite3.connect(path)
        connection.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY,version INTEGER,active INTEGER,data TEXT); CREATE UNIQUE INDEX single_active ON tasks(active) WHERE active=1;")
        task = encode(Task("legacy", 3, "pending"))
        for field in ("kind", "device_id", "ac_constraints", "ac_plan", "ac_approval"):
            task.pop(field)
        connection.execute("INSERT INTO tasks VALUES ('legacy',3,1,?)", (json.dumps(task),))
        connection.commit()
        connection.close()
        migrated = Repository(path)
        self.assertEqual(migrated.active_task().version, 3)
        migrated.create_ac_task(self.c, Tariff())
        self.assertEqual(len(migrated.active_tasks()), 2)
        with migrated.transaction() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_independent_devices_and_clock_guard(self):
        ac = self.proposed_ac()
        wash = self.repo.create_task(TaskConstraints(self.c.start, self.c.end, None, None), WashProgram(), Tariff())
        self.assertEqual(len(self.repo.active_tasks()), 2)
        with self.assertRaises(ValueError):
            self.repo.create_ac_task(self.c, Tariff())
        with self.assertRaises(ValueError):
            self.repo.switch_clock(60)
        self.repo.update_task(ac.task_id, ac.version, status="failed")
        self.assertEqual(self.repo.active_task().task_id, wash.task_id)

    def test_ac_approval_execution_shutdown_dedup_and_recovery(self):
        task = self.proposed_ac()
        task = self.repo.approve_ac(task.task_id, task.version)
        real = self.real + timedelta(minutes=5)
        command = ACCommand("start", "ac-1", "start", self.clock.clock_id, real + timedelta(seconds=5), self.ac.boot_id,
            task.task_id, task.version, "run", task.ac_plan, task.ac_approval)
        self.assertFalse(self.ac.handle(replace(command, command_id="bad", approval=None), real).accepted)
        self.assertTrue(self.ac.handle(command, real).accepted)
        self.assertTrue(self.ac.handle(command, real).accepted)
        self.assertFalse(self.ac.handle(replace(command, command_id="another"), real).accepted)
        state = self.ac.tick(real + timedelta(minutes=30))
        self.assertLessEqual(state.temperature_c, 28)
        self.assertGreater(state.energy_kwh, 0)
        restarted = VirtualAC(self.device_repo, self.clock, real + timedelta(minutes=30))
        self.assertEqual(restarted.state.status, "fault")
        self.assertEqual(restarted.state.energy_kwh, state.energy_kwh)
        self.assertEqual(self.ac.tick(self.real + timedelta(hours=2)).status, "completed")
        cold = self.ac.state.temperature_c
        state = self.ac.tick(self.real + timedelta(hours=3))
        self.assertEqual(state.power_w, 0)
        self.assertGreater(state.temperature_c, cold)

    def test_ac_controller_and_in_scope_reschedule(self):
        task = self.proposed_ac()
        task = self.repo.approve_ac(task.task_id, task.version)
        within = replace(task.ac_plan, plan_id="updated", constraints=replace(self.c, start=self.c.start + timedelta(minutes=1)))
        within = make_ac_plan(within.constraints, 30, Tariff(), self.clock.clock_id)
        task = self.repo.propose_ac(task.task_id, task.version, within)
        self.assertEqual(task.status, "scheduled")
        outside = make_ac_plan(replace(self.c, end=self.c.end + timedelta(hours=1)), 30, Tariff(), self.clock.clock_id)
        task = self.repo.propose_ac(task.task_id, task.version, outside)
        self.assertIsNone(task.ac_approval)
        self.assertEqual(task.status, "pending")
        task = self.repo.approve_ac(task.task_id, task.version)
        transport = FakeTransport()
        control = ACController(self.repo, transport, self.hems, self.real)
        now = self.real + timedelta(minutes=5)
        transport.inbox.append(("state", self.ac.tick(now)))
        control.tick(now)
        cmd = next(cmd for _, cmd in transport.sent if cmd.action == "start")
        self.assertEqual(cmd.device_id, "ac-1")
        self.assertTrue(self.ac.handle(cmd, now).accepted)
        transport.inbox.append(("state", self.ac.tick(now + timedelta(seconds=1))))
        control.tick(now + timedelta(seconds=1))
        self.assertEqual(self.repo.active_task("ac-1").status, "running")

    def test_exact_ac_energy_and_speed_independence(self):
        a = cooling_energy(40, 27, 3600)
        # Independently integrate the instantaneous teaching-model power.
        numeric = sum(min(1200, max(100, 100 + 300 * 13 * math.exp(-(i + .5) / 6000))) * .1 for i in range(36000)) / 3_600_000
        self.assertAlmostEqual(a, numeric, places=7)
        from smart_home_energy.domain import LogicalClock
        real = utcnow()
        for speed in (1, 60):
            clock = LogicalClock.create(speed, real)
            elapsed = (clock.now(real + timedelta(seconds=3600 / speed)) - clock.logical_base).total_seconds()
            self.assertAlmostEqual(predict(self.c, 30, Tariff(), elapsed)[1], cooling_energy(30, 27, 3600))

    def tariff(self, plan, baseline=100):
        configure_tariff(self.repo, {"plan": plan, "baseline_kwh": {f"2026-{m:02}": baseline for m in range(1, 13)}})
        return selected_tariff(self.repo)

    def test_residential_cross_tier_no_fake_time_savings(self):
        tariff = self.tariff("residential", 119.9)
        day = datetime(2026, 10, 19, 10, tzinfo=TAIPEI)
        cost = tariff.cost(day, 3600, 200)
        self.assertAlmostEqual(cost, .1 * 1.78 + .1 * 2.26)
        self.assertAlmostEqual(cost, tariff.cost(day + timedelta(hours=12), 3600, 200))
        p = make_plan(day, WashProgram(), tariff, self.clock.clock_id)
        self.assertAlmostEqual(p.cost, .1 * 1.78 + .125 * 2.26)
        self.assertEqual(decode(type(p), encode(p)), p)

    def test_time_tariffs_season_midnight_holidays_and_surcharge(self):
        tariff = self.tariff("tou2", 1999.9)
        day = datetime(2026, 6, 2, 8, 30, tzinfo=TAIPEI)
        self.assertAlmostEqual(tariff.cost(day, 3600, 200), .1 * 2.06 + .1 * 5.16 + .1 * 1.04)
        self.assertEqual(tariff.rate(datetime(2026, 6, 19, 12, tzinfo=TAIPEI)), 2.06)
        # Official offpeak-day calendar does NOT treat compensatory Monday Apr 6 as a full offpeak day.
        self.assertEqual(tariff.rate(datetime(2026, 4, 6, 15, tzinfo=TAIPEI)), 4.93)
        tariff = self.tariff("tou3")
        self.assertEqual(tariff.rate(datetime(2026, 6, 2, 17, tzinfo=TAIPEI)), 7.13)
        self.assertEqual(tariff.rate(datetime(2026, 10, 19, 17, tzinfo=TAIPEI)), 4.48)
        self.assertAlmostEqual(tariff.cost(datetime(2026, 5, 31, 23, 30, tzinfo=TAIPEI), 3600, 200), .1 * 1.99 + .1 * 2.06)
        with self.assertRaises(ValueError):
            tariff.rate(datetime(2028, 1, 3, 12, tzinfo=TAIPEI))

    def test_quote_changes_require_recalculation_and_combined_cost(self):
        tariff = self.tariff("residential", 119.9)
        c = TaskConstraints(self.clock.now() + timedelta(minutes=5), self.clock.now() + timedelta(hours=4), None, None)
        tasks = []
        task = self.repo.create_task(c, WashProgram(), tariff)
        task = self.repo.propose(task.task_id, task.version, c, make_plan(c.earliest, WashProgram(), tariff, self.clock.clock_id))
        ac = self.repo.create_ac_task(self.c, tariff)
        ac = self.repo.propose_ac(ac.task_id, ac.version, make_ac_plan(self.c, 30, tariff, self.clock.clock_id))
        self.repo.approve(task.task_id, task.version, 10)
        with self.assertRaisesRegex(ValueError, "已改變"):
            self.repo.approve_ac(ac.task_id, ac.version)
        self.assertAlmostEqual(household_estimate(self.repo), task.plan.cost)
        self.tariff("residential", 350)
        with self.assertRaises(ValueError):
            self.repo.check_quote(task)

    def test_real_subagent_loops_shared_budget_and_repeated_delegation(self):
        self.repo.event("household", None, {"root_id": "root", "text": "洗衣並開冷氣；明確日期條件由測試提供"})
        event = self.repo.claim_event()
        wc = encode(TaskConstraints(self.c.start, self.c.end, None, None))
        def washer_proposal(messages):
            p = json.loads(messages[-1]["content"])["tool_result"]
            return json.dumps(action("propose_plan", plan=p, constraints=wc, expected_version=1))
        def ac_proposal(messages):
            p = json.loads(messages[-1]["content"])["tool_result"]
            return json.dumps(action("propose_plan", plan=p, expected_version=1))
        replies = [action("get_home_state"), action("delegate_tasks", requests=[{"device_id": "washer-1", "text": "洗衣"}, {"device_id": "ac-1", "text": "冷氣"}]),
                   action("get_device_context"), action("find_schedule", constraints=wc), washer_proposal,
                   action("get_device_context"), action("find_plan", constraints=encode(self.c)), ac_proposal]
        model = FakeModel(replies)
        run = asyncio.run(run_household(self.repo, event, self.hems, [model]))
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.requests, 8)
        self.assertEqual(len(self.repo.active_tasks()), 2)
        self.assertTrue(all(t.status == "pending" for t in self.repo.active_tasks()))
        children = [json.loads(r["data"]) for r in self.repo.records("agent_runs") if json.loads(r["data"]).get("parent_id")]
        self.assertEqual({c["role"] for c in children}, {"Washer Agent", "AC Agent"})
        again = FakeModel(replies[:2])
        run = asyncio.run(run_household(self.repo, event, self.hems, [again]))
        self.assertEqual(run.requests, 2)
        self.assertEqual(len(self.repo.active_tasks()), 2)

    def test_subagent_fallback_preserves_parent_context_and_stays_on_backup(self):
        from smart_home_energy.agent import ProviderUnavailable
        self.repo.event("fallback", None, {"root_id": "fallback", "text": "冷氣"})
        event = self.repo.claim_event()
        first = FakeModel([action("get_home_state"), ProviderUnavailable("vllm offline")])
        first.name = "vllm"
        backup = FakeModel([action("delegate_tasks", requests=[{"device_id": "ac-1", "text": "需要補問時段"}]), action("get_device_context"), {"ask": "請提供冷氣時段"}])
        backup.name = "azure"
        run = asyncio.run(run_household(self.repo, event, self.hems, [first, backup]))
        self.assertEqual(run.requests, 5)
        self.assertEqual(run.provider, "azure")
        self.assertEqual(run.status, "needs_details")
        self.assertTrue(any("tool_result" in m["content"] for m in backup.contexts[0]))
        self.assertEqual(len(first.contexts), 2)

    def test_ac_impossible_comfort_and_outdated_model_work_cannot_propose(self):
        from smart_home_energy.ac_domain import validate_ac
        from smart_home_energy.multi_agent import DeviceTools
        from smart_home_energy.agent import ProviderUnavailable
        p = make_ac_plan(replace(self.c, end=self.c.start + timedelta(minutes=1)), 40, Tariff(), self.clock.clock_id)
        with self.assertRaises(ValueError):
            validate_ac(p, self.clock.now(), self.clock.clock_id)
        task = self.proposed_ac()
        tools = DeviceTools(self.repo, task.task_id)
        self.repo.update_task(task.task_id, task.version, status="cancelled")
        with self.assertRaises(ProviderUnavailable):
            tools.call("propose_plan", {"plan": encode(task.ac_plan), "expected_version": task.version})

    def test_actual_energy_ledger_keeps_partial_run_and_deduplicates_samples(self):
        from smart_home_energy.ac import ACState
        self.tariff("residential", 119.9)
        task = self.repo.create_ac_task(self.c, selected_tariff(self.repo))
        task = self.repo.propose_ac(task.task_id, task.version, make_ac_plan(self.c, 30, task.tariff, self.clock.clock_id))
        task = self.repo.approve_ac(task.task_id, task.version)
        task = self.repo.update_task(task.task_id, task.version, status="running", run_id="measured")
        s = ACState("boot", 1, self.real, self.c.start, self.clock.clock_id, task_id=task.task_id, run_id="measured")
        self.repo.save_device(s)
        s = replace(s, sequence=2, logical_at=self.c.start + timedelta(minutes=10), energy_kwh=.2)
        self.repo.save_device(s)
        self.repo.save_device(s)
        self.repo.update_task(task.task_id, task.version, status="aborted")
        self.assertEqual(len(self.repo.records("energy_segments")), 1)
        self.assertAlmostEqual(household_estimate(self.repo), .1 * 1.78 + .1 * 2.26)

    def test_simulated_household_total_excludes_unapproved_proposals(self):
        task = self.proposed_ac()
        self.assertEqual(household_estimate(self.repo), 0)
        task = self.repo.approve_ac(task.task_id, task.version)
        self.assertAlmostEqual(household_estimate(self.repo), task.ac_plan.cost)
        self.assertAlmostEqual(household_estimate(self.repo, Tariff(peak=8, offpeak=8)), task.ac_plan.energy_kwh * 8)
        self.repo.update_task(task.task_id, task.version, status="cancelled")
        self.assertEqual(household_estimate(self.repo), 0)

    def test_ac_runtime_alerts_preserve_running(self):
        task = self.proposed_ac()
        task = self.repo.approve_ac(task.task_id, task.version)
        task = self.repo.update_task(task.task_id, task.version, status="running", run_id="comfort")
        real = self.real + timedelta(minutes=30)
        transport = FakeTransport()
        controller = ACController(self.repo, transport, self.hems, self.real)
        state = replace(self.ac.state, status="running", task_id=task.task_id, run_id="comfort", temperature_c=29,
                        observed_at=real, logical_at=self.clock.now(real))
        self.repo.save_device(state)
        controller.reconcile(real)
        self.assertIsNone(self.repo.meta("anomaly:" + task.task_id))
        real += timedelta(minutes=2)
        state = replace(state, sequence=state.sequence + 1, observed_at=real, logical_at=self.clock.now(real))
        self.repo.save_device(state)
        controller.reconcile(real)
        self.assertIn("舒適範圍", self.repo.meta("anomaly:" + task.task_id)["reason"])
        self.repo.save_device(replace(state, sequence=state.sequence + 1, cost=100))
        controller.reconcile(real)
        self.assertIn("沒有因費用關機", self.repo.meta("anomaly:" + task.task_id)["reason"])
        self.assertEqual(self.repo.task(task.task_id).status, "running")
        self.assertEqual(self.repo.records("commands"), [])
        event = self.repo.claim_event()
        self.repo.finish_event(event.event_id)
        controller.reconcile(real)
        self.assertIsNone(self.repo.claim_event())

    def test_ac_unreachable_comfort_at_start_records_anomaly(self):
        c = replace(self.c, end=self.c.start + timedelta(minutes=5))
        task = self.repo.create_ac_task(c, Tariff())
        task = self.repo.propose_ac(task.task_id, task.version, make_ac_plan(c, 27, task.tariff, self.clock.clock_id))
        task = self.repo.approve_ac(task.task_id, task.version)
        real = self.real + timedelta(minutes=5)
        self.repo.save_device(replace(self.ac.state, temperature_c=40, observed_at=real, logical_at=self.clock.now(real)))
        controller = ACController(self.repo, FakeTransport(), self.hems, self.real)
        controller.recovered = True
        controller.execute_due(real)
        self.assertEqual(self.repo.task(task.task_id).status, "scheduled")
        self.assertIn("無法達到舒適範圍", self.repo.meta("anomaly:" + task.task_id)["reason"])
        self.assertEqual(self.repo.records("commands"), [])

    def test_ac_cost_splits_at_wall_clock_boundary(self):
        start = datetime(2026, 10, 17, 21, 59, 45, tzinfo=TAIPEI)
        c = ACConstraints(start, start + timedelta(seconds=90))
        _, energy, cost = predict(c, 30, Tariff())
        first = cooling_energy(30, 27, 15)
        self.assertAlmostEqual(cost, first * 6 + (energy - first) * 2.5)

    def test_budget_exhaustion_preserves_partial_success(self):
        self.repo.event("household", None, {"root_id": "limit", "text": "兩台設備"})
        event = self.repo.claim_event()
        wc = encode(TaskConstraints(self.c.start, self.c.end, None, None))
        def propose(messages):
            return json.dumps(action("propose_plan", plan=json.loads(messages[-1]["content"])["tool_result"], constraints=wc, expected_version=1))
        model = FakeModel([action("delegate_tasks", requests=[{"device_id": "washer-1", "text": "洗衣"}, {"device_id": "ac-1", "text": "冷氣"}]),
            action("find_schedule", constraints=wc), propose])
        run = asyncio.run(run_household(self.repo, event, replace(self.hems, agent_requests=3), [model]))
        self.assertEqual(run.requests, 3)
        self.assertEqual(run.status, "partial_success")
        self.assertEqual(self.repo.active_task().status, "pending")
        self.assertEqual(self.repo.active_task("ac-1").status, "needs_details")


if __name__ == "__main__":
    unittest.main()

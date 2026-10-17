from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_home_energy.config import HemsSettings
from smart_home_energy.control import Controller
from smart_home_energy.domain import TaskConstraints, WashProgram, Tariff, make_plan, utcnow
from smart_home_energy.repository import Repository
from smart_home_energy.washer import Washer, DeviceCommand


class FakeTransport:
    connected = True

    def __init__(self):
        self.inbox, self.sent = [], []

    def drain(self):
        messages, self.inbox = self.inbox, []
        return messages

    def publish(self, kind, value):
        self.sent.append((kind, value))
        return self.connected


class ControlTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(directory / "control.db")
        self.device_repo = Repository(directory / "device.db")
        self.clock = self.repo.clock()
        self.real = utcnow()
        self.hems = replace(HemsSettings.from_env(), database=self.repo.path, device_database=self.device_repo.path)
        self.washer = Washer(self.device_repo, self.clock, self.real)
        self.transport = FakeTransport()
        self.mono = 0
        self.control = Controller(self.repo, self.transport, self.hems, self.real, lambda: self.mono)

    def scheduled(self):
        start = self.clock.now(self.real) + timedelta(seconds=2)
        c = TaskConstraints(start, start + timedelta(hours=3), None, None)
        task = self.repo.create_task(c, WashProgram(), Tariff())
        plan = make_plan(start, task.program, task.tariff, self.clock.clock_id)
        task = self.repo.propose(task.task_id, task.version, c, plan)
        return self.repo.approve(task.task_id, task.version, 10)

    def state(self, real):
        self.transport.inbox.append(("state", self.washer.tick(real)))

    def start(self):
        task = self.scheduled()
        self.real += timedelta(seconds=2)
        self.state(self.real)
        self.control.tick(self.real)
        commands = [v for k, v in self.transport.sent if v.action == "start"]
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertTrue(self.washer.handle(command, self.real).accepted)
        self.real += timedelta(seconds=1)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertEqual(self.repo.active_task().status, "running")
        return command

    def test_duplicate_start_and_completion(self):
        command = self.start()
        original_run = self.washer.state.run_id
        self.assertTrue(self.washer.handle(command, self.real).accepted)
        self.assertEqual(self.washer.state.run_id, original_run)
        self.assertFalse(self.washer.handle(replace(command, command_id="new"), self.real).accepted)
        self.real += timedelta(hours=1)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertIsNone(self.repo.active_task())
        self.assertAlmostEqual(self.washer.state.energy_kwh, .225)

    def test_abort_requires_confirmation_and_device_state(self):
        self.start()
        task = self.repo.active_task()
        p = {"task_id": task.task_id, "expected_version": task.version}
        with self.assertRaises(ValueError):
            self.control.handle_request("abort", p, self.real)
        self.control.handle_request("abort", {**p, "confirmed": True}, self.real)
        self.assertEqual(self.repo.active_task().status, "stopping")
        command = self.transport.sent[-1][1]
        self.washer.handle(command, self.real)
        self.real += timedelta(seconds=1)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertEqual(self.repo.recent_tasks()[0]["status"], "aborted")

    def test_controller_restart_no_second_start_and_device_restart_fault(self):
        self.start()
        self.control = Controller(self.repo, self.transport, self.hems, self.real)
        self.real += timedelta(seconds=1)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertEqual(self.repo.active_task().status, "running")
        self.assertEqual(len([v for _, v in self.transport.sent if v.action == "start"]), 1)
        self.washer = Washer(self.device_repo, self.clock, self.real)
        self.assertEqual(self.washer.state.status, "fault")
        self.real += timedelta(seconds=1)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertEqual(self.repo.active_task().status, "failed")

    def test_stale_and_unapproved_never_start(self):
        task = self.scheduled()
        self.repo.update_task(task.task_id, task.version, status="pending", approval=None)
        self.real += timedelta(seconds=3)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertFalse(any(v.action == "start" for _, v in self.transport.sent))
        bad = DeviceCommand("bad", "washer-1", "start", self.clock.clock_id, self.real + timedelta(seconds=5), self.washer.boot_id)
        self.assertFalse(self.washer.handle(bad, self.real).accepted)

    def test_ui_request_deduplication(self):
        task = self.scheduled()
        self.repo.submit("cancel", "cancel", {"task_id": task.task_id, "expected_version": task.version})
        self.control.tick(self.real)
        self.control.tick(self.real)
        self.assertEqual(self.repo.recent_tasks()[0]["status"], "cancelled")
        self.assertEqual(self.repo.recent_tasks()[0]["version"], task.version + 1)

    def test_confirmation_timeout_queries_without_replaying_start(self):
        self.scheduled()
        self.real += timedelta(seconds=2)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertEqual(self.repo.active_task().status, "starting")
        # A real transport publish succeeds but the appliance never accepts it.
        self.mono = 6
        self.real += timedelta(seconds=6)
        self.state(self.real)
        self.control.tick(self.real)
        self.assertEqual(self.repo.active_task().status, "unknown")
        sent = [v.action for _, v in self.transport.sent]
        self.assertEqual(sent.count("start"), 1)
        self.assertIn("query", sent)
        event = self.repo.claim_event()
        self.assertIsNotNone(event)
        self.repo.finish_event(event.event_id)
        self.control.tick(self.real)
        self.assertIsNone(self.repo.claim_event())

    def test_expired_clock_boot_and_unapproved_cost_are_rejected(self):
        task = self.scheduled()
        self.real += timedelta(seconds=2)
        base = DeviceCommand("expired", "washer-1", "start", self.clock.clock_id,
            self.real - timedelta(seconds=1), self.washer.boot_id, task.task_id, task.version, "run", task.plan, task.approval)
        self.assertFalse(self.washer.handle(base, self.real).accepted)
        for change in ({"clock_id": "other"}, {"expected_boot": "old"}, {"approval": replace(task.approval, max_cost=0)}):
            cmd = replace(base, command_id=str(change), expires_at=self.real + timedelta(seconds=5), **change)
            self.assertFalse(self.washer.handle(cmd, self.real).accepted)
        self.assertEqual(self.washer.state.status, "idle")

    def test_changed_requirement_suspends_old_approved_start(self):
        task = self.scheduled()
        self.repo.submit("change", "details", {"task_id": task.task_id, "expected_version": task.version, "text": "改成明天"})
        self.real += timedelta(seconds=2)
        self.state(self.real)
        self.control.tick(self.real)
        changed = self.repo.active_task()
        self.assertEqual(changed.status, "needs_details")
        self.assertIsNone(changed.plan)
        self.assertEqual(changed.approval, task.approval)
        self.assertFalse(any(c.action == "start" for _, c in self.transport.sent))


if __name__ == "__main__":
    unittest.main()

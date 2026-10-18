import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from smart_home_energy.ac import VirtualAC
from smart_home_energy.ac_control import ACController
from smart_home_energy.assistant_defaults import preferences, save_preferences
from smart_home_energy.assistant_ui import conversations
from smart_home_energy.config import HemsSettings
from smart_home_energy.control import Controller
from smart_home_energy.domain import encode, decode, utcnow, WashProgram, TaskConstraints, make_plan, Tariff, TAIPEI
from smart_home_energy.multi_agent import run_household, DeviceTools
from smart_home_energy.repository import Repository
from smart_home_energy.store import TelemetryStore
from test_agent import FakeModel, action
from test_control import FakeTransport


class AssistantTests(unittest.TestCase):
    def setUp(self):
        st.cache_resource.clear()
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(directory / "control.db")
        self.hems = replace(HemsSettings.from_env(), database=self.repo.path, device_database=str(directory / "washer.db"))
        self.enterContext(patch("smart_home_energy.hems_ui.HemsSettings.from_env", return_value=self.hems))
        subscriber = self.enterContext(patch("smart_home_energy.mqtt_transport.TelemetryClient"))
        subscriber.return_value.store = TelemetryStore()
        self.provider = self.enterContext(patch("smart_home_energy.agent.SDKProvider.complete"))
        self.real = utcnow()
        self.repo.set_meta("controller_heartbeat", self.real.isoformat())
        self.ac = VirtualAC(Repository(directory / "ac.db"), self.repo.clock(), self.real)
        self.repo.save_device(self.ac.state)
        self.control = Controller(self.repo, FakeTransport(), self.hems)
        self.ac_control = ACController(self.repo, FakeTransport(), self.hems)
        self.app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src/smart_home_energy/dashboard.py"), default_timeout=15)

    def tearDown(self):
        st.cache_resource.clear()

    def button(self, label):
        return next(b for b in self.app.button if b.label == label)

    def default_ac(self):
        task = self.repo.create_ac_task(None, self.hems.tariff())
        tools = DeviceTools(self.repo, task.task_id)
        plan = tools.call("prepare_default_plan", {})
        return decode(type(task), tools.call("propose_plan", {"plan": plan, "expected_version": task.version}))

    def test_shortcut_persists_one_request_and_waits_for_agent(self):
        self.app.run()
        self.assertEqual([t.label for t in self.app.tabs], ["冷氣", "洗衣機", "用電紀錄"])
        self.button("開冷氣 1 小時").click().run()
        self.app.run()
        self.assertEqual(len(self.repo.pending_requests()), 1)
        self.assertIsNone(self.repo.active_task("ac-1"))
        self.assertTrue(self.button("安排洗衣").disabled)
        self.control.process_requests(utcnow())
        self.assertTrue(conversations(self.repo)[0]["pending"])
        self.assertEqual(self.repo.records("commands"), [])
        self.provider.assert_not_called()
        self.assertEqual(len(self.app.exception), 0)

    def test_defaults_delegate_both_devices_and_require_separate_confirmation(self):
        self.repo.event("household:defaults", None, {"root_id": "defaults", "text": "幫我開冷氣並安排洗衣"})
        def washer(messages):
            value = json.loads(messages[-1]["content"])["tool_result"]
            return json.dumps(action("propose_plan", plan=value["plan"], constraints=value["constraints"], expected_version=1))
        def ac(messages):
            value = json.loads(messages[-1]["content"])["tool_result"]
            return json.dumps(action("propose_plan", plan=value, expected_version=1))
        model = FakeModel([action("get_home_state"), action("delegate_tasks", requests=[{"device_id": "washer-1", "text": "安排洗衣"}, {"device_id": "ac-1", "text": "開冷氣"}]),
                           action("get_device_context"), action("find_default_schedule"), washer,
                           action("get_device_context"), action("prepare_default_plan"), ac])
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [model]))
        self.assertEqual(run.status, "completed")
        cold, wash = self.repo.active_task("ac-1"), self.repo.active_task()
        self.assertTrue(cold.ac_plan.start_on_approval)
        self.assertEqual((cold.ac_constraints.end - cold.ac_constraints.start).total_seconds(), 3600)
        self.assertEqual((wash.constraints.quiet_start, wash.constraints.quiet_end), (1380, 420))
        self.assertLessEqual((wash.constraints.deadline - self.repo.clock().now()).total_seconds(), 86400)
        self.app.run()
        self.button("確認開啟冷氣").click().run()
        self.ac_control.process_requests(utcnow())
        self.assertEqual(self.repo.active_task("ac-1").status, "scheduled")
        self.assertEqual(self.repo.active_task().status, "pending")
        self.assertEqual(self.repo.records("commands"), [])
        self.provider.assert_not_called()
        self.assertEqual(len(self.app.exception), 0)

    def test_immediate_ac_rebases_on_confirmation_and_still_shuts_down(self):
        task = self.default_ac()
        old_start = task.ac_constraints.start
        later = self.real + timedelta(minutes=10)
        self.ac.tick(later)
        self.repo.save_device(self.ac.state)
        with patch("smart_home_energy.domain.utcnow", return_value=later), patch("smart_home_energy.repository.utcnow", return_value=later):
            task = self.repo.approve_ac(task.task_id, task.version)
        self.assertGreater(task.ac_constraints.start, old_start + timedelta(minutes=9))
        self.assertFalse(task.ac_plan.start_on_approval)
        self.assertEqual((task.ac_constraints.end - task.ac_constraints.start).total_seconds(), 3600)
        from smart_home_energy.ac import ACCommand
        start_real = later + timedelta(seconds=2)
        command = ACCommand("relative", "ac-1", "start", self.repo.clock().clock_id, start_real + timedelta(seconds=5), self.ac.boot_id,
                            task.task_id, task.version, "run", task.ac_plan, task.ac_approval)
        self.assertTrue(self.ac.handle(command, start_real).accepted)
        self.assertEqual(self.ac.tick(start_real + timedelta(hours=1)).status, "completed")

    def test_ac_edit_suspends_existing_plan_and_preserves_conversation(self):
        task = self.default_ac()
        self.app.run()
        self.button("修改計畫").click().run()
        self.app.text_input(key=f"edit_text_{task.task_id}").set_value("改成兩小時")
        self.button("請助理調整").click().run()
        self.ac_control.process_requests(utcnow())
        current = self.repo.task(task.task_id)
        self.assertEqual(current.status, "needs_details")
        self.assertIsNone(current.ac_plan)
        event = self.repo.claim_event()
        self.assertEqual(event.task_id, task.task_id)
        self.assertEqual(conversations(self.repo)[0]["text"], "改成兩小時")
        self.assertEqual(self.repo.records("commands"), [])

    def test_running_card_requires_second_confirmation_to_stop(self):
        task = self.default_ac()
        task = self.repo.approve_ac(task.task_id, task.version)
        task = self.repo.update_task(task.task_id, task.version, status="running", run_id="running")
        state = replace(self.ac.state, task_id=task.task_id, run_id="running", status="running", observed_at=utcnow())
        self.repo.save_device(state)
        self.app.run()
        self.button("關閉冷氣").click().run()
        self.assertEqual(self.repo.pending_requests(), [])
        self.button("確認停止").click().run()
        self.ac_control.process_requests(utcnow())
        self.assertEqual(self.repo.task(task.task_id).status, "stopping")
        self.assertEqual(json.loads(self.repo.records("commands")[0]["data"])["action"], "stop")
        self.assertEqual(len(self.app.exception), 0)

    def test_preferences_are_validated_and_explicit_dates_remain_fixed(self):
        prefs = {**preferences(self.repo), "target_c": 26.5, "duration_minutes": 90}
        save_preferences(self.repo, prefs)
        task = self.default_ac()
        self.assertEqual(task.ac_plan.constraints.target_c, 26.5)
        self.assertEqual((task.ac_constraints.end - task.ac_constraints.start).total_seconds(), 5400)
        self.repo.update_task(task.task_id, task.version, status="cancelled")
        from smart_home_energy.ac_domain import ACConstraints, make_ac_plan
        start = self.repo.clock().now() + timedelta(hours=2)
        c = ACConstraints(start, start + timedelta(hours=1))
        task = self.repo.create_ac_task(c, self.hems.tariff())
        task = self.repo.propose_ac(task.task_id, task.version, make_ac_plan(c, 30, task.tariff, self.repo.clock().clock_id))
        approved = self.repo.approve_ac(task.task_id, task.version)
        self.assertEqual(approved.ac_constraints.start, start)
        with self.assertRaises(ValueError):
            save_preferences(self.repo, {**prefs, "duration_minutes": 0})

    def test_formal_tariff_requires_baseline_and_keeps_other_months(self):
        self.app.run()
        next(box for box in self.app.selectbox if box.label == "電價方案").set_value("residential")
        self.button("儲存費率設定").click().run()
        self.assertTrue(any("請先輸入" in error.value for error in self.app.error))
        self.assertEqual(self.repo.pending_requests(), [])
        next(field for field in self.app.number_input if field.label == "該月基準用電（度）").set_value(350)
        self.button("儲存費率設定").click().run()
        self.control.process_requests(utcnow())
        first_month = self.repo.clock().now().astimezone(TAIPEI).strftime("%Y-%m")
        another = (self.repo.clock().now().astimezone(TAIPEI).replace(day=28) + timedelta(days=4)).date().replace(day=1)
        next(field for field in self.app.date_input if field.label == "基準用電所屬月份").set_value(another).run()
        next(field for field in self.app.number_input if field.label == "該月基準用電（度）").set_value(200)
        self.button("儲存費率設定").click().run()
        self.control.process_requests(utcnow())
        self.assertEqual(self.repo.meta("tariff_settings")["baseline_kwh"], {first_month: 350, another.strftime("%Y-%m"): 200})


    def test_new_request_after_finished_task_cannot_reuse_telemetry_task_id(self):
        old = self.default_ac()
        old = self.repo.update_task(old.task_id, old.version, status="aborted", run_id="old")
        self.repo.save_device(replace(self.ac.state, task_id=old.task_id, run_id="old", status="aborted"))
        self.repo.event("household:again", None, {"root_id": "again", "text": "再幫我開冷氣"})
        def proposal(messages):
            return json.dumps(action("propose_plan", plan=json.loads(messages[-1]["content"])["tool_result"], expected_version=1))
        model = FakeModel([action("get_home_state"),
            action("delegate_tasks", requests=[{"device_id": "ac-1", "text": "開冷氣", "task_id": old.task_id, "expected_version": old.version}]),
            action("delegate_tasks", requests=[{"device_id": "ac-1", "text": "開冷氣"}]),
            action("prepare_default_plan"), proposal])
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [model]))
        self.assertEqual(run.status, "completed")
        self.assertNotEqual(self.repo.active_task("ac-1").task_id, old.task_id)
        self.assertEqual(self.repo.task(old.task_id).status, "aborted")
        home = json.loads(model.contexts[1][-1]["content"])["tool_result"]
        self.assertIsNone(home["devices"]["ac-1"]["task_id"])
        self.assertIn("已結束任務", json.loads(model.contexts[2][-1]["content"])["error"])
        self.assertEqual(self.repo.records("commands"), [])


if __name__ == "__main__":
    unittest.main()

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from smart_home_energy.config import HemsSettings
from smart_home_energy.control import Controller
from smart_home_energy.repository import Repository
from smart_home_energy.domain import utcnow
from smart_home_energy.store import TelemetryStore
from test_control import FakeTransport

DASHBOARD = Path(__file__).resolve().parents[1] / "src/smart_home_energy/dashboard.py"


class WasherUITests(unittest.TestCase):
    def setUp(self):
        st.cache_resource.clear()
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.hems = replace(HemsSettings.from_env(), database=str(directory / "control.db"), device_database=str(directory / "washer.db"))
        self.repo = Repository(self.hems.database)
        self.enterContext(patch("smart_home_energy.hems_ui.HemsSettings.from_env", return_value=self.hems))
        subscriber = self.enterContext(patch("smart_home_energy.mqtt_transport.TelemetryClient"))
        subscriber.return_value.store = TelemetryStore()
        self.app = AppTest.from_file(str(DASHBOARD), default_timeout=15)
        self.control = Controller(self.repo, FakeTransport(), self.hems)

    def tearDown(self):
        st.cache_resource.clear()

    def button(self, label):
        return next(b for b in self.app.button if b.label == label)

    def test_form_proposal_approval_cancel_and_rerun_idempotency(self):
        self.app.run()
        self.assertEqual(len(self.app.exception), 0)
        self.button("計算並提出計畫").click().run()
        self.app.run()
        self.assertEqual(len(self.repo.pending_requests()), 1)
        self.assertIsNone(self.repo.active_task())  # UI does not create a scheduler.
        self.control.process_requests(utcnow())
        task = self.repo.active_task()
        self.assertEqual(task.status, "pending")
        self.app.run()
        self.assertFalse(self.button("核准此版本計畫").disabled)
        self.assertTrue(self.button("套用時間模式").disabled)
        self.button("核准此版本計畫").click().run()
        self.control.tick()
        self.assertEqual(self.repo.active_task().status, "scheduled")
        self.app.run()
        self.button("取消未啟動任務").click().run()
        self.control.tick()
        self.assertIsNone(self.repo.active_task())
        self.app.run()
        self.assertEqual(len(self.app.exception), 0)

    def test_natural_language_persists_one_event_without_ui_model_calls(self):
        self.app.run()
        self.app.text_area[0].set_value("明天早上八點前洗完，晚上十一點後保持安靜")
        self.button("交給 Main Agent").click().run()
        self.app.run()
        self.assertEqual(len(self.repo.pending_requests()), 1)
        self.control.tick()
        self.assertEqual(self.repo.active_task().status, "needs_details")
        event = self.repo.claim_event()
        self.assertIn("八點", event.payload["text"])
        self.assertIsNone(self.repo.claim_event())
        self.assertEqual(self.repo.records("agent_runs"), [])


if __name__ == "__main__":
    unittest.main()

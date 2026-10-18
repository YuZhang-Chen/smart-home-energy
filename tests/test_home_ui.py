from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from smart_home_energy.ac import VirtualAC
from smart_home_energy.ac_control import ACController
from smart_home_energy.config import HemsSettings
from smart_home_energy.control import Controller
from smart_home_energy.domain import TAIPEI, utcnow
from smart_home_energy.repository import Repository
from smart_home_energy.store import TelemetryStore
from smart_home_energy.tariffs import source_snapshot
from test_control import FakeTransport


class HomeUITests(unittest.TestCase):
    def setUp(self):
        st.cache_resource.clear()
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.hems = replace(HemsSettings.from_env(), database=str(directory / "control.db"), device_database=str(directory / "washer.db"))
        self.repo = Repository(self.hems.database)
        self.enterContext(patch("smart_home_energy.hems_ui.HemsSettings.from_env", return_value=self.hems))
        subscriber = self.enterContext(patch("smart_home_energy.mqtt_transport.TelemetryClient"))
        subscriber.return_value.store = TelemetryStore()
        self.provider = self.enterContext(patch("smart_home_energy.agent.SDKProvider.complete"))
        self.ac = VirtualAC(Repository(directory / "ac.db"), self.repo.clock())
        self.repo.save_device(self.ac.state)
        self.control = Controller(self.repo, FakeTransport(), self.hems)
        self.ac_control = ACController(self.repo, FakeTransport(), self.hems)
        self.app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src/smart_home_energy/dashboard.py"), default_timeout=15)

    def tearDown(self):
        st.cache_resource.clear()

    def button(self, label):
        return next(b for b in self.app.button if b.label == label)

    def test_ac_form_independent_approval_cancel_and_no_ui_executor(self):
        self.app.run()
        self.button("計算冷氣計畫").click().run()
        self.app.run()
        self.assertIsNone(self.repo.active_task("ac-1"))
        self.assertEqual(len(self.repo.pending_requests()), 1)
        self.ac_control.process_requests(utcnow())
        self.assertEqual(self.repo.active_task("ac-1").status, "pending")
        self.app.run()
        self.assertTrue(self.button("套用時間模式").disabled)
        self.button("確認開啟冷氣").click().run()
        self.ac_control.tick()
        self.assertEqual(self.repo.active_task("ac-1").status, "scheduled")
        self.app.run()
        self.button("取消計畫").click().run()
        self.ac_control.tick()
        self.assertIsNone(self.repo.active_task("ac-1"))
        self.provider.assert_not_called()
        self.assertEqual(len(self.app.exception), 0)

    def test_household_request_and_tariff_settings_are_durable(self):
        self.app.run()
        self.app.chat_input(key="assistant_input").set_value("今晚洗衣和開冷氣").run()
        self.app.run()
        self.assertEqual(len(self.repo.pending_requests()), 1)
        self.control.process_requests(utcnow())
        event = self.repo.claim_event()
        self.assertIsNone(event.task_id)
        self.assertIn("冷氣", event.payload["text"])
        self.assertIsNone(self.repo.claim_event())
        self.app.run()
        next(box for box in self.app.selectbox if box.label == "電價方案").set_value("residential")
        month = utcnow().astimezone(TAIPEI).strftime("%Y-%m")
        next(field for field in self.app.number_input if field.label == "該月基準用電（度）").set_value(350)
        self.button("儲存費率設定").click().run()
        self.control.process_requests(utcnow())
        self.assertEqual(self.repo.meta("tariff_settings")["plan"], "residential")
        self.provider.assert_not_called()

    def test_failed_source_refresh_keeps_existing_version(self):
        source = source_snapshot()
        self.repo.set_meta("tariff_source", source)
        with patch("smart_home_energy.home_ui.refresh_source", side_effect=OSError("offline")):
            self.app.run()
            self.button("下載並查核台電費率").click().run()
        self.assertEqual(self.repo.meta("tariff_source"), source)
        self.assertTrue(any("保留既有來源" in error.value for error in self.app.error))
        self.assertEqual(len(self.app.exception), 0)


if __name__ == "__main__":
    unittest.main()

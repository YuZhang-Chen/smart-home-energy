import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from smart_home_energy.models import Telemetry
from smart_home_energy.store import TelemetryStore
from smart_home_energy.config import HemsSettings

DASHBOARD = Path(__file__).resolve().parents[1] / "src/smart_home_energy/dashboard.py"


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        st.cache_resource.clear()
        directory = Path(self.enterContext(TemporaryDirectory()))
        hems = replace(HemsSettings.from_env(), database=str(directory / "control.db"), device_database=str(directory / "washer.db"))
        self.enterContext(patch("smart_home_energy.hems_ui.HemsSettings.from_env", return_value=hems))

    def tearDown(self) -> None:
        st.cache_resource.clear()

    def test_waiting_has_no_fake_zeroes(self) -> None:
        with patch("smart_home_energy.mqtt_transport.TelemetryClient") as client:
            client.return_value.store = TelemetryStore()
            app = AppTest.from_file(str(DASHBOARD), default_timeout=15).run()
        self.assertEqual(len(app.exception), 0)
        labels = {"室內溫度 (°C)", "冷氣即時功率 (W)", "本次累積用電 (kWh)"}
        self.assertEqual([metric.value for metric in app.metric if metric.label in labels], ["—", "—", "—"])
        self.assertTrue(any("等待模擬器" in info.value for info in app.info))

    def test_values_staleness_and_single_subscriber_across_reruns(self) -> None:
        store = TelemetryStore()
        store.set_connected(True)
        item = Telemetry("run-ui", 2, datetime.now(UTC) - timedelta(seconds=30),
                         "ac-1", 29.75, 925, 0.00123456)
        store.add(item)
        with patch("smart_home_energy.mqtt_transport.TelemetryClient") as client:
            client.return_value.store = store
            app = AppTest.from_file(str(DASHBOARD), default_timeout=15).run()
            app.run()
            self.assertEqual(client.call_count, 1)
            client.return_value.start.assert_called_once()
        self.assertEqual(len(app.exception), 0)
        labels = {"室內溫度 (°C)", "冷氣即時功率 (W)", "本次累積用電 (kWh)"}
        self.assertEqual([metric.value for metric in app.metric if metric.label in labels],
                         ["29.75", "925.0", "0.001235"])
        self.assertTrue(any("超過 10 秒" in warning.value for warning in app.warning))


if __name__ == "__main__":
    unittest.main()

import json
import math
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from smart_home_energy.config import Settings
from smart_home_energy.models import Telemetry


def sample(**changes: object) -> Telemetry:
    base = Telemetry("run-1", 0, datetime.now(UTC), "ac-1", 30.0, 1000.0, 0.0)
    return replace(base, **changes)


class TelemetryTests(unittest.TestCase):
    def test_json_round_trip_and_utc(self) -> None:
        original = sample(timestamp=datetime(2026, 10, 16, 8, tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(Telemetry.from_json(original.to_json()), original)
        self.assertTrue(original.to_dict()["timestamp"].endswith("Z"))
        self.assertEqual(Telemetry.from_json(original.to_json().encode()), original)

    def test_bad_payloads_are_rejected(self) -> None:
        data = sample().to_dict()
        payloads = ["{", "null", "[]", "{}", b"\xff", json.dumps({**data, "extra": 1})]
        for key, value in (
            ("run_id", ""), ("device_id", None), ("sequence", True),
            ("sequence", -1), ("sequence", 1.5), ("timestamp", 42),
            ("timestamp", "2026-10-16T08:00:00"), ("timestamp", "bad-date"),
            ("temperature_c", math.nan), ("power_w", math.inf),
            ("power_w", -1), ("power_w", "1000"), ("power_w", True),
            ("energy_kwh", -0.1),
        ):
            payloads.append(json.dumps({**data, key: value}))
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                Telemetry.from_json(payload)


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.env_file = Path(directory) / ".env"
        self.env_file.write_text(
            "MQTT_HOST=127.0.0.2\nMQTT_PORT=1884\nMQTT_TOPIC=test/from-file\n",
            encoding="utf-8",
        )
        self.enterContext(patch("smart_home_energy.config.ENV_FILE", self.env_file))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_loads_settings_from_dotenv(self) -> None:
        self.assertEqual(Settings.from_env(), Settings("127.0.0.2", 1884, "test/from-file"))

    def test_existing_environment_takes_precedence(self) -> None:
        with patch.dict(os.environ, {
            "MQTT_HOST": " localhost ", "MQTT_PORT": "1884", "MQTT_TOPIC": "test/ac",
        }):
            self.assertEqual(Settings.from_env(), Settings("localhost", 1884, "test/ac"))

    def test_missing_dotenv_and_settings_have_clear_error(self) -> None:
        self.env_file.unlink()
        with self.assertRaisesRegex(ValueError, "缺少設定.*MQTT_TOPIC"):
            Settings.from_env()

    def test_incomplete_dotenv_has_no_hardcoded_fallback(self) -> None:
        self.env_file.write_text("MQTT_HOST=127.0.0.1\nMQTT_PORT=1883\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "缺少設定：MQTT_TOPIC"):
            Settings.from_env()

    def test_invalid_settings(self) -> None:
        for changes in (
            {"MQTT_HOST": " "}, {"MQTT_PORT": "abc"}, {"MQTT_PORT": "0"},
            {"MQTT_PORT": "65536"}, {"MQTT_TOPIC": "home/#"},
            {"MQTT_TOPIC": "home/+"}, {"MQTT_TOPIC": ""},
        ):
            with self.subTest(changes=changes), patch.dict(os.environ, changes, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()


if __name__ == "__main__":
    unittest.main()

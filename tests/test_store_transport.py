import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import paho.mqtt.client as mqtt
from paho.mqtt.reasoncodes import ReasonCode

from smart_home_energy.config import Settings
from smart_home_energy.models import Telemetry
from smart_home_energy.mqtt_transport import TelemetryClient
from smart_home_energy.store import TelemetryStore


def sample(sequence: int = 0, run_id: str = "run-1") -> Telemetry:
    return Telemetry(run_id, sequence, datetime.now(UTC), "ac-1", 30, 1000, sequence / 1000)


class StoreTests(unittest.TestCase):
    def test_empty_bounded_and_detached_snapshot(self) -> None:
        store = TelemetryStore(max_samples=3)
        self.assertIsNone(store.snapshot().latest)
        store.add(sample())
        before = store.snapshot()
        for sequence in range(1, 6):
            store.add(sample(sequence))
        self.assertEqual([item.sequence for item in store.snapshot().samples], [3, 4, 5])
        self.assertEqual(len(before.samples), 1)

    def test_new_run_resets_history_and_duplicates_are_ignored(self) -> None:
        store = TelemetryStore()
        store.add(sample(2))
        store.add(sample(2))
        store.add(sample(1))
        self.assertEqual(len(store.snapshot().samples), 1)
        store.add(sample(0, "run-2"))
        self.assertEqual(len(store.snapshot().samples), 1)
        self.assertEqual(store.snapshot().latest.energy_kwh, 0)
        self.assertEqual(store.snapshot().latest.run_id, "run-2")

    def test_stale_uses_source_timestamp_including_retained_messages(self) -> None:
        store = TelemetryStore()
        self.assertFalse(store.snapshot().is_stale(datetime.now(UTC)))
        item = sample()
        store.add(item)
        self.assertFalse(store.snapshot().is_stale(item.timestamp + timedelta(seconds=10)))
        self.assertTrue(store.snapshot().is_stale(item.timestamp + timedelta(seconds=11)))


class TransportTests(unittest.TestCase):
    def test_bad_message_does_not_block_next_valid_message(self) -> None:
        store = TelemetryStore()
        transport = TelemetryClient(Settings("127.0.0.1", 1883, "test/ac"), store)
        message = mqtt.MQTTMessage()
        message.payload = b"not-json"
        with self.assertLogs("smart_home_energy.mqtt_transport", level="WARNING"):
            transport._on_message(transport.client, None, message)
        self.assertIsNone(store.snapshot().latest)
        item = sample()
        message.payload = item.to_json().encode()
        transport._on_message(transport.client, None, message)
        self.assertEqual(store.snapshot().latest, item)

    def test_successful_reconnect_resubscribes(self) -> None:
        store = TelemetryStore()
        transport = TelemetryClient(Settings("127.0.0.1", 1883, "test/ac"), store)
        client = Mock()
        reason = ReasonCode(mqtt.PacketTypes.CONNACK, "Success")
        for _ in range(2):
            transport._on_connect(client, None, mqtt.ConnectFlags(False), reason, None)
        self.assertEqual(client.subscribe.call_count, 2)
        self.assertTrue(store.snapshot().connected)

    def test_publish_offline_never_queues_a_snapshot(self) -> None:
        transport = TelemetryClient(Settings("127.0.0.1", 1883, "test/ac"))
        with patch.object(transport.client, "publish") as publish:
            self.assertFalse(transport.publish(sample()))
            publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()

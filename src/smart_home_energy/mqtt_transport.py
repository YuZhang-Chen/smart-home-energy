"""MQTT lifecycle and the single boundary for untrusted messages."""

import logging
from uuid import uuid4

import paho.mqtt.client as mqtt
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from smart_home_energy.config import Settings
from smart_home_energy.models import Telemetry
from smart_home_energy.store import TelemetryStore

logger = logging.getLogger(__name__)


class TelemetryClient:
    """A long-lived publisher, or a subscriber when a store is supplied."""

    def __init__(self, settings: Settings, store: TelemetryStore | None = None) -> None:
        self.settings = settings
        self.store = store
        role = "subscriber" if store is not None else "publisher"
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"energy-{role}-{uuid4().hex[:12]}",
            protocol=mqtt.MQTTv311,
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self) -> None:
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=10)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()
        if self.store is not None:
            self.store.set_connected(False)

    def publish(self, sample: Telemetry) -> bool:
        # QoS 0 snapshots are deliberately not queued while offline.
        if not self.client.is_connected():
            return False
        result = self.client.publish(
            self.settings.mqtt_topic, sample.to_json(), qos=0, retain=True
        )
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def _on_connect(
        self, client: mqtt.Client, userdata: object, flags: mqtt.ConnectFlags,
        reason_code: ReasonCode, properties: Properties | None,
    ) -> None:
        connected = reason_code == 0
        if self.store is not None:
            self.store.set_connected(connected)
        if not connected:
            logger.error("Broker rejected connection: %s", reason_code)
            return
        logger.info("Connected to MQTT %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)
        if self.store is not None:
            client.subscribe(self.settings.mqtt_topic, qos=0)

    def _on_connect_fail(self, client: mqtt.Client, userdata: object) -> None:
        logger.warning("MQTT broker unavailable; Paho will retry automatically")

    def _on_disconnect(
        self, client: mqtt.Client, userdata: object, flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode, properties: Properties | None,
    ) -> None:
        if self.store is not None:
            self.store.set_connected(False)
        logger.info("MQTT disconnected: %s", reason_code)

    def _on_message(self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        if self.store is None:
            return
        try:
            sample = Telemetry.from_json(message.payload)
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            logger.warning("Ignored invalid telemetry: %s", error)
            return
        self.store.add(sample)

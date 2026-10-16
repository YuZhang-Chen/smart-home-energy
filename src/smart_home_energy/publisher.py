"""Run the simulator and publish its current state every two seconds."""

import logging
import signal
import time
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

from smart_home_energy.config import Settings
from smart_home_energy.models import Telemetry
from smart_home_energy.mqtt_transport import TelemetryClient
from smart_home_energy.simulator import AirConditioner

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 2.0


def run(settings: Settings, stop: Event) -> None:
    transport = TelemetryClient(settings)
    state = AirConditioner()
    run_id = uuid4().hex
    sequence = 0
    previous = time.monotonic()
    transport.start()
    try:
        while not stop.is_set():
            tick_started = time.monotonic()
            if sequence:
                state = state.step(tick_started - previous)
            previous = tick_started
            sample = Telemetry(
                run_id=run_id, sequence=sequence, timestamp=datetime.now(UTC),
                device_id="ac-1", temperature_c=state.temperature_c,
                power_w=state.power_w, energy_kwh=state.energy_kwh,
            )
            sent = transport.publish(sample)
            logger.info("%s %s", "Published" if sent else "Offline snapshot", sample.to_json())
            sequence += 1
            stop.wait(max(0.0, INTERVAL_SECONDS - (time.monotonic() - tick_started)))
    finally:
        transport.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = Settings.from_env()
    except ValueError as error:
        raise SystemExit(f"設定錯誤：{error}") from error
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run(settings, stop)


if __name__ == "__main__":
    main()

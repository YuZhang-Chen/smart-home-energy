"""Real-broker acceptance check. --recovery restarts this project's broker."""

import argparse
import json
import logging
import subprocess
import time
from pathlib import Path
from threading import Event, Thread

from smart_home_energy.config import Settings
from smart_home_energy.mqtt_transport import TelemetryClient
from smart_home_energy.publisher import run
from smart_home_energy.store import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for MQTT state")
        time.sleep(0.1)


def broker(action: str) -> None:
    subprocess.run(["docker", "compose", action, "mosquitto"], cwd=ROOT, check=True, timeout=40)


def start_publisher(settings: Settings) -> tuple[Event, Thread]:
    stop = Event()
    thread = Thread(target=run, args=(settings, stop), name="smoke-publisher")
    thread.start()
    return stop, thread


def stop_publisher(stop: Event, thread: Thread) -> None:
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), "Publisher failed to shut down"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery", action="store_true", help="also test broker restart and late startup")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    defaults = Settings.from_env()
    # Keep smoke traffic separate from the demonstration topic.
    settings = Settings(defaults.mqtt_host, defaults.mqtt_port, defaults.mqtt_topic + "/smoke")
    if args.recovery:
        broker("stop")
    store = TelemetryStore()
    subscriber = TelemetryClient(settings, store)
    subscriber.start()
    stop, thread = start_publisher(settings)
    report: dict[str, object] = {}
    try:
        if args.recovery:
            time.sleep(3)
            assert thread.is_alive() and not store.snapshot().connected
            assert store.snapshot().latest is None
            broker("start")
            report["late_broker_start"] = "passed"
        wait_until(lambda: store.snapshot().latest is not None)
        first = store.snapshot().latest
        print("Collecting real MQTT telemetry for 30 seconds...", flush=True)
        started = time.monotonic()
        wait_until(lambda: time.monotonic() - started >= 30, timeout=35)
        samples = [item for item in store.snapshot().samples if item.sequence >= first.sequence]
        assert len(samples) >= 14, f"Only received {len(samples)} samples"
        intervals = [(b.timestamp - a.timestamp).total_seconds() for a, b in zip(samples, samples[1:])]
        assert all(1.5 <= interval <= 3.5 for interval in intervals), intervals
        for previous, current in zip(samples, samples[1:]):
            assert current.sequence == previous.sequence + 1
            assert current.energy_kwh > previous.energy_kwh
            assert current.temperature_c < previous.temperature_c
        report.update({
            "steady_state_seconds": round(time.monotonic() - started, 2),
            "samples": len(samples),
            "interval_min_seconds": min(intervals),
            "interval_max_seconds": max(intervals),
            "latest_telemetry": samples[-1].to_dict(),
        })
        before = store.snapshot().latest
        subscriber.client.publish(settings.mqtt_topic, b"not-json", qos=0).wait_for_publish(timeout=3)
        wait_until(lambda: store.snapshot().latest.sequence > before.sequence)
        report["invalid_message_recovery"] = "passed"

        if args.recovery:
            before = store.snapshot().latest
            broker("stop")
            wait_until(lambda: not store.snapshot().connected)
            time.sleep(3)
            broker("start")
            wait_until(lambda: store.snapshot().connected and store.snapshot().latest.sequence > before.sequence + 1)
            after = store.snapshot().latest
            assert after.run_id == before.run_id and after.energy_kwh > before.energy_kwh
            report["broker_restart"] = "passed"
            report["offline_sequence_gap"] = after.sequence - before.sequence

        before = store.snapshot().latest
        stop_publisher(stop, thread)
        stop, thread = start_publisher(settings)
        wait_until(lambda: store.snapshot().latest.run_id != before.run_id)
        restarted = store.snapshot()
        assert len({item.run_id for item in restarted.samples}) == 1
        assert restarted.latest.energy_kwh < before.energy_kwh
        report["publisher_restart_and_history_reset"] = "passed"
        report["result"] = "passed"
    finally:
        stop_publisher(stop, thread)
        subscriber.stop()
        if args.recovery:
            broker("start")
    output = ROOT / "artifacts/mqtt-smoke.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

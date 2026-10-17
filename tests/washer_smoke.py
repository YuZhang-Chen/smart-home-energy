"""Real Mosquitto + separate appliance/controller processes. --recovery restarts broker."""

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from smart_home_energy.config import Settings
from smart_home_energy.domain import TaskConstraints, encode, decode
from smart_home_energy.repository import Repository
from smart_home_energy.washer import DeviceCommand, WasherTransport

ROOT = Path(__file__).resolve().parents[1]


def wait(predicate, seconds=25):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.15)
    raise AssertionError("Timed out waiting for washer acceptance condition")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery", action="store_true")
    args = parser.parse_args()
    directory = ROOT / "artifacts" / ("washer-smoke-" + uuid4().hex[:10])
    directory.mkdir(parents=True)
    env = {**os.environ, "HEMS_DB": str(directory / "control.sqlite3"),
           "HEMS_DEVICE_DB": str(directory / "device.sqlite3"),
           "HEMS_MQTT_ROOT": "home/hems/smoke/" + uuid4().hex,
           "PYTHONUNBUFFERED": "1"}
    for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT", "VLLM_BASE_URL", "VLLM_API_KEY", "VLLM_MODEL"):
        env[key] = ""
    # Standard 60-minute program, regardless of local demonstration customizations.
    for key, value in {"WASH_MINUTES": "30", "RINSE_MINUTES": "15", "SPIN_MINUTES": "15", "WASH_POWER_W": "200", "RINSE_POWER_W": "100", "SPIN_POWER_W": "400"}.items():
        env[key] = value
    repo = Repository(env["HEMS_DB"])
    repo.switch_clock(60)
    log = (directory / "services.log").open("w", encoding="utf-8")

    def start(module):
        return subprocess.Popen([sys.executable, "-m", "smart_home_energy." + module], cwd=ROOT, env=env, stdout=log, stderr=log)

    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    def broker(action):
        subprocess.run(["docker", "compose", action, "mosquitto"], cwd=ROOT, check=True, timeout=40)

    def state():
        return repo.device() or {}

    def submit(kind, payload):
        identifier = uuid4().hex
        repo.submit(identifier, kind, payload)
        wait(lambda: any(r["id"] == identifier and r["status"] != "pending" for r in repo.records("requests")))
        record = next(r for r in repo.records("requests") if r["id"] == identifier)
        assert record["status"] == "done", record
        return identifier

    def create_and_approve():
        clock = repo.clock()
        now = clock.now()
        c = TaskConstraints(now + timedelta(minutes=3), now + timedelta(hours=4), None, None)
        request_id = submit("create_form", {"constraints": encode(c)})
        task = repo.active_task()
        assert task.status == "pending" and task.plan
        p = {"task_id": task.task_id, "expected_version": task.version, "max_cost": 10}
        approve_id = submit("approve", p)
        repo.submit(approve_id, "approve", p)
        wait(lambda: repo.active_task() is not None and repo.active_task().status == "running", seconds=12)
        return task.task_id

    device, control = start("washer"), start("control")
    probe = WasherTransport(Settings.from_env(), env["HEMS_MQTT_ROOT"], "control")
    probe.start()
    report = {"real_broker": True, "real_models": False, "database": env["HEMS_DB"], "device_database": env["HEMS_DEVICE_DB"], "mqtt_root": env["HEMS_MQTT_ROOT"]}
    try:
        wait(lambda: state().get("clock_id") == repo.clock().clock_id and probe.connected)
        print("Starting standard 60-minute wash at 60x...", flush=True)
        task_id = create_and_approve()
        started = time.monotonic()
        run_id = state()["run_id"]
        commands = [decode(DeviceCommand, json.loads(r["data"])) for r in repo.records("commands")]
        command = next(c for c in commands if c.action == "start" and c.task_id == task_id)
        probe.publish("command", command)
        time.sleep(.5)
        assert state()["run_id"] == run_id
        report["duplicate_command"] = "passed"

        # Restart controller while appliance continues autonomously.
        stop(control)
        control = start("control")
        wait(lambda: repo.active_task().status == "running")
        report["controller_restart"] = "passed"
        if args.recovery:
            before = state()["energy_kwh"]
            print("Restarting real Broker during wash...", flush=True)
            broker("stop")
            time.sleep(4)
            assert device.poll() is None
            broker("start")
            wait(lambda: state().get("energy_kwh", 0) > before and state().get("status") == "running")
            assert state()["run_id"] == run_id
            report["broker_restart_and_offline_progress"] = "passed"
        wait(lambda: repo.active_task() is None, seconds=80)
        completed = next(t for t in repo.recent_tasks() if t["task_id"] == task_id)
        assert completed["status"] == "completed"
        assert abs(state()["energy_kwh"] - .225) < 1e-12
        assert 50 <= time.monotonic() - started <= 75
        report["wash_real_seconds"] = round(time.monotonic() - started, 3)
        report["energy_kwh"] = state()["energy_kwh"]
        report["cost"] = state()["cost"]
        starts = [json.loads(r["data"]) for r in repo.records("commands", 100)]
        assert sum(c["action"] == "start" and c["task_id"] == task_id for c in starts) == 1
        print("Completion verified. Testing physical process restart and manual reset...", flush=True)
        task_id = create_and_approve()
        stop(device)
        device = start("washer")
        wait(lambda: repo.active_task().status == "failed")
        assert state()["status"] == "fault"
        task = repo.active_task()
        submit("reset", {"task_id": task.task_id, "expected_version": task.version, "confirmed": True})
        wait(lambda: repo.active_task() is None)
        report["device_restart_fault_and_manual_reset"] = "passed"
        print("Testing confirmed abort...", flush=True)
        create_and_approve()
        task = repo.active_task()
        submit("abort", {"task_id": task.task_id, "expected_version": task.version, "confirmed": True})
        wait(lambda: repo.active_task() is None)
        assert repo.recent_tasks()[0]["status"] == "aborted"
        report["confirmed_abort"] = "passed"
        report["result"] = "passed"
    finally:
        stop(control)
        stop(device)
        probe.stop()
        log.close()
        if args.recovery:
            broker("start")
        (ROOT / "artifacts/washer-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()

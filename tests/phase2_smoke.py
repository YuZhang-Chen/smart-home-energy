"""Real broker/appliance processes; Matter mode must traverse the Matter wire.

Use an isolated broker (18884 by default). --recovery stops/restarts ONLY the
explicitly named test container, never the application's normal broker.
"""
import argparse
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from smart_home_energy.ac_control import ACController
from smart_home_energy.bulb_control import BulbController
from smart_home_energy.ac_domain import ACConstraints, make_ac_plan
from smart_home_energy.config import Settings, HemsSettings
from smart_home_energy.control import Controller
from smart_home_energy.domain import TAIPEI, TaskConstraints, WashProgram, make_plan, decode, utcnow
from smart_home_energy.repository import Repository
from smart_home_energy.washer import DeviceCommand, WasherTransport
from smart_home_energy.tariffs import configure_tariff, selected_tariff


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["mqtt", "matter"], default="matter")
    parser.add_argument("--port", type=int, default=18884)
    parser.add_argument("--recovery", action="store_true")
    parser.add_argument("--broker-container", default="hems-phase2-mosquitto")
    args = parser.parse_args()
    if args.recovery and not args.broker_container.startswith("hems-phase2-"):
        parser.error("Recovery requires a dedicated hems-phase2-* test container")
    root = Path(__file__).resolve().parents[1]
    directory = root / "artifacts" / ("phase2-" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    settings = Settings("127.0.0.1", args.port, "home/energy/phase2/" + directory.name)
    hems = replace(HemsSettings.from_env(), database=str(directory / "control.db"),
                   device_database=str(directory / "washer.db"), mqtt_root="home/hems/" + directory.name)
    repo = Repository(hems.database)
    repo.switch_clock(60)
    env = os.environ.copy()
    env.update(MQTT_HOST=settings.mqtt_host, MQTT_PORT=str(settings.mqtt_port), MQTT_TOPIC=settings.mqtt_topic,
               HEMS_DB=hems.database, HEMS_DEVICE_DB=hems.device_database, HEMS_AC_DB=str(directory / "ac.db"),
               HEMS_BULB_DB=str(directory / "bulb.db"),
               HEMS_MQTT_ROOT=hems.mqtt_root, HEMS_MATTER_STORAGE=str(directory / "matter"), HEMS_MATTER_PORT="5550",
               HEMS_MATTER_DISCRIMINATOR="3831", PYTHONIOENCODING="utf-8")
    processes, logs, checks = [], [], {}
    hub, transports, controllers, bridge = None, [], [], None

    def spawn(name):
        log = (directory / f"{name}-{len(processes)}.log").open("w", encoding="utf-8")
        logs.append(log)
        command = ["node", str(root / "matter/bridge.mjs")] if name == "bridge" else [sys.executable, "-m", "smart_home_energy." + name]
        process = subprocess.Popen(command, cwd=root / "matter" if name == "bridge" else root, env=env, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)
        return process

    def stop(process):
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    def tick():
        for controller in controllers:
            controller.tick()

    def wait(predicate, label, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            tick()
            if predicate():
                checks[label] = True
                print(label + ": passed", flush=True)
                return
            time.sleep(.25)
        raise AssertionError(f"Timed out: {label}; tasks={[(t.device_id,t.status,t.message) for t in repo.active_tasks()]}; logs={directory}")

    def connect():
        nonlocal hub, transports, controllers
        if args.transport == "matter":
            from smart_home_energy.matter_transport import MatterHub, MatterTransport
            os.environ.update({key: env[key] for key in ("HEMS_MATTER_STORAGE", "HEMS_MATTER_PORT", "HEMS_MATTER_DISCRIMINATOR")})
            hub = MatterHub(repo, settings, hems)
            transports = [MatterTransport(hub, d) for d in ("washer-1", "ac-1", "bulb-1")]
        else:
            transports = [WasherTransport(settings, hems.mqtt_root, "control", d) for d in ("washer-1", "ac-1", "bulb-1")]
        for transport in transports:
            transport.start()
        controllers = [Controller(repo, transports[0], hems), ACController(repo, transports[1], hems),
                       BulbController(repo, transports[2])]

    def set_bulb(label, **target):
        identifier = "smoke-bulb-" + label
        repo.submit(identifier, "set_bulb", {"device_id": "bulb-1", "duration_seconds": 0, **target})
        wait(lambda: repo.request(identifier)["status"] in {"done", "error"}, "bulb_" + label, 20)
        request = repo.request(identifier)
        if request["status"] != "done":
            raise AssertionError(f"Bulb action failed: {label}: {request['result']}")
        action = repo.bulb_action(identifier)
        if not action or action["status"] != "confirmed":
            raise AssertionError(f"Bulb action not confirmed: {label}: {action}")
        return repo.device("bulb-1")

    def schedule_washer(official=False):
        now = repo.clock().now()
        if official:
            configure_tariff(repo, {"plan": "tou2", "baseline_kwh": {now.astimezone(TAIPEI).strftime('%Y-%m'): 350}})
        tariff = selected_tariff(repo, hems.tariff())
        c = TaskConstraints(now + timedelta(minutes=4), now + timedelta(hours=4), None, None)
        task = repo.create_task(c, WashProgram(), tariff)
        task = repo.propose(task.task_id, task.version, c, make_plan(c.earliest, task.program, tariff, repo.clock().clock_id))
        return repo.approve(task.task_id, task.version, 10)

    try:
        washer = spawn("washer")
        spawn("ac")
        spawn("bulb")
        if args.transport == "matter":
            bridge = spawn("bridge")
            time.sleep(3)
        connect()
        wait(lambda: all(t.connected for t in transports) and all(repo.device(d) for d in ("washer-1", "ac-1", "bulb-1")), "live_device_feedback", 65)
        state = set_bulb("on", power=True)
        assert state["power"] and state["level"] == 254 and state["mired"] == 370
        state = set_bulb("warm_40", power=True, brightness_percent=40, kelvin=2700)
        assert state["power"] and state["level"] == 102 and state["mired"] == 370
        state = set_bulb("off", power=False)
        assert not state["power"] and state["level"] == 102 and state["mired"] == 370
        state = set_bulb("color_while_off", kelvin=6500)
        assert not state["power"] and state["level"] == 102 and state["mired"] == 154
        state = set_bulb("on_retains_settings", power=True)
        assert state["power"] and state["level"] == 102 and state["mired"] == 154
        checks["bulb_feedback"] = {"power": state["power"], "level": state["level"], "mired": state["mired"]}
        first = schedule_washer()
        c = ACConstraints(first.plan.start, first.plan.start + timedelta(minutes=65), target_c=26.5)
        ac = repo.create_ac_task(c, selected_tariff(repo, hems.tariff()))
        ac = repo.propose_ac(ac.task_id, ac.version, make_ac_plan(c, repo.device("ac-1")["temperature_c"], ac.tariff, repo.clock().clock_id))
        repo.approve_ac(ac.task_id, ac.version)
        began = time.monotonic()
        wait(lambda: all(repo.active_task(d) and repo.active_task(d).status == "running" for d in ("washer-1", "ac-1")), "two_devices_running", 20)
        original = next(decode(DeviceCommand, json.loads(row["data"])) for row in repo.records("commands", 100) if json.loads(row["data"])["action"] == "start" and json.loads(row["data"])["device_id"] == "washer-1")
        # Exact application command ID: a retransmission must not create a run.
        if original.expires_at > utcnow():
            transports[0].publish("command", original)
        run_id = repo.device()["run_id"]
        controllers = [Controller(repo, transports[0], hems), ACController(repo, transports[1], hems),
                       BulbController(repo, transports[2])]
        wait(lambda: controllers[0].recovered and controllers[1].recovered, "controller_recovery", 10)
        if args.transport == "matter" and original.expires_at > utcnow():
            # A reused ID with changed execution context must not replace the
            # bridge's persistent result or reach the MQTT appliance.
            transports[0].publish("command", replace(original, run_id="conflicting-context"))
            until = time.monotonic() + 1.5
            while time.monotonic() < until:
                tick(); time.sleep(.1)
            journal = json.loads((directory / "matter/bridge-commands.json").read_text(encoding="utf-8"))
            assert journal[original.command_id]["command"]["run_id"] == run_id
            assert journal[original.command_id]["result"]["accepted"]
            checks["conflicting_duplicate_rejected"] = True
        if args.recovery:
            before = repo.device()["energy_kwh"]
            subprocess.run(["docker", "stop", args.broker_container], check=True, capture_output=True)
            until = time.monotonic() + 4
            while time.monotonic() < until:
                tick(); time.sleep(.25)
            subprocess.run(["docker", "start", args.broker_container], check=True, capture_output=True)
            wait(lambda: repo.device()["run_id"] == run_id and repo.device()["energy_kwh"] > before, "broker_recovery_same_run", 20)
        if args.transport == "matter":
            for transport in transports:
                transport.stop()
            connect()
            wait(lambda: all(t.connected for t in transports) and repo.device()["run_id"] == run_id and controllers[0].recovered, "matter_controller_restart", 40)
        wait(lambda: repo.task(first.task_id).status == "completed" and repo.task(ac.task_id).status == "completed", "washer_and_ac_completed", 90)
        assert abs(repo.device()["energy_kwh"] - .225) < 1e-9
        assert abs(repo.device("ac-1")["target_c"] - 26.5) < 1e-9
        assert repo.device("ac-1")["power_w"] == 0
        checks["energy_kwh"] = repo.device()["energy_kwh"]
        checks["completion_real_seconds"] = time.monotonic() - began
        start_count = sum(json.loads(r["data"])["action"] == "start" and json.loads(r["data"])["device_id"] == "washer-1" for r in repo.records("commands", 100))
        assert start_count == 1
        if args.transport == "matter":
            stop(bridge)
            wait(lambda: not transports[0].connected, "matter_offline_detected", 30)
            bulb_before = repo.device("bulb-1").copy()
            repo.submit("smoke-bulb-offline", "set_bulb", {"device_id": "bulb-1", "power": False})
            wait(lambda: repo.request("smoke-bulb-offline")["status"] == "error", "bulb_no_mqtt_bypass", 10)
            assert repo.device("bulb-1")["state_version"] == bulb_before["state_version"]
            held = schedule_washer()
            until = time.monotonic() + 7
            while time.monotonic() < until:
                tick(); time.sleep(.25)
            assert repo.device()["run_id"] == run_id
            assert repo.task(held.task_id).status == "scheduled"
            checks["no_mqtt_bypass"] = True
            repo.update_task(held.task_id, held.version, status="cancelled")
            bridge = spawn("bridge")
            wait(lambda: all(t.connected for t in transports) and all(controllers[i].recovered for i in range(2)), "matter_bridge_restart", 65)
            state = set_bulb("after_bridge_restart", brightness_percent=40, kelvin=2700)
            assert state["power"] and state["level"] == 102 and state["mired"] == 370
        second = schedule_washer(official=True)
        wait(lambda: repo.task(second.task_id).status == "running", "official_tariff_command", 20)
        task = repo.task(second.task_id)
        controllers[0].handle_request("abort", {"task_id": task.task_id, "expected_version": task.version, "confirmed": True}, utcnow())
        wait(lambda: repo.task(second.task_id).status == "aborted", "manual_abort_confirmed", 15)
        third = schedule_washer()
        wait(lambda: repo.task(third.task_id).status == "running", "third_run_started", 20)
        stop(washer)
        washer = spawn("washer")
        wait(lambda: repo.task(third.task_id).status == "failed", "appliance_restart_fault", 20)
        task = repo.task(third.task_id)
        controllers[0].handle_request("reset", {"task_id": task.task_id, "expected_version": task.version, "confirmed": True}, utcnow())
        wait(lambda: repo.task(third.task_id).status == "resolved", "manual_reset_confirmed", 15)
        report = {"passed": True, "transport": args.transport, "checks": checks, "directory": str(directory)}
        (root / "artifacts" / f"phase2-{args.transport}-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    finally:
        for transport in transports:
            transport.stop()
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        if args.recovery:
            subprocess.run(["docker", "start", args.broker_container], capture_output=True)


if __name__ == "__main__":
    main()

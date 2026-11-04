"""Opt-in real-model Copilot Runtime -> Python Agent -> Matter appliance E2E.

Requires configured live model credentials and an isolated MQTT broker used by
the Matter bridge. Every database, MQTT topic and Matter fabric is unique to
this run; the model may only propose plans, which this test explicitly approves
through the same REST API used by the React approval cards.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from uuid import uuid4

import httpx

from smart_home_energy.repository import Repository


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mqtt-port", type=int, default=18884)
    args = parser.parse_args()

    run_id = "copilot-live-" + uuid4().hex[:10]
    directory = ROOT / "artifacts" / run_id
    directory.mkdir(parents=True)
    api_port, runtime_port = free_port(), free_port()
    # Matter commissioning discriminators are 12-bit values. The project's
    # established isolated Matter smoke port is 5550/5551.
    matter_port, discriminator = 5550, 3831
    repo = Repository(directory / "control.db")
    repo.switch_clock(60)
    env = os.environ.copy()
    env.update({
        "HEMS_DB": str(directory / "control.db"),
        "HEMS_DEVICE_DB": str(directory / "washer.db"),
        "HEMS_AC_DB": str(directory / "ac.db"),
        "HEMS_BULB_DB": str(directory / "bulb.db"),
        "HEMS_TRANSPORT": "matter",
        "HEMS_MQTT_ROOT": "test/hems/" + run_id,
        "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(args.mqtt_port),
        "MQTT_TOPIC": "test/energy/" + run_id,
        "HEMS_MATTER_STORAGE": str(directory / "matter"),
        "HEMS_MATTER_PORT": str(matter_port),
        "HEMS_MATTER_DISCRIMINATOR": str(discriminator),
        "HEMS_COPILOT_RUNTIME_PORT": str(runtime_port),
        "HEMS_PYTHON_API_URL": f"http://127.0.0.1:{api_port}",
        "HEMS_COPILOT_RUNTIME_URL": f"http://127.0.0.1:{runtime_port}",
        "WASH_MINUTES": "2", "RINSE_MINUTES": "2", "SPIN_MINUTES": "2",
        "PYTHONIOENCODING": "utf-8",
    })
    processes, logs = [], []
    checks = {}

    def spawn(name, command, cwd=ROOT):
        log = (directory / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)
        return process

    client = httpx.Client(timeout=httpx.Timeout(180, connect=3), trust_env=False)
    api = f"http://127.0.0.1:{api_port}"
    runtime = f"http://127.0.0.1:{runtime_port}"

    def healthy(url, path):
        response = client.get(url + path, timeout=2)
        return response.status_code == 200

    def wait(predicate, label, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            dead = [p.args for p in processes if p.poll() is not None]
            if dead:
                raise AssertionError(f"service exited; inspect logs in {directory}")
            try:
                value = predicate()
                if value:
                    checks[label] = True
                    print(label + ": passed", flush=True)
                    return value
            except (httpx.TransportError, ValueError, KeyError):
                pass
            time.sleep(.25)
        raise AssertionError(f"timed out: {label}; see {directory}")

    def post_request(kind, payload):
        body = {"request_id": run_id + ":" + uuid4().hex[:12], "kind": kind, "payload": payload}
        response = client.post(api + "/api/requests", json=body, timeout=5)
        assert response.status_code == 202, response.text
        wait(lambda: (lambda value: value if value["status"] in {"done", "error"} else None)(
            client.get(api + "/api/requests/" + body["request_id"], timeout=5).json()),
            "request_" + kind, 20)
        result = client.get(api + "/api/requests/" + body["request_id"], timeout=5).json()
        assert result["status"] == "done", result
        return result

    try:
        # Start simulated appliances and Matter bridge, then the resident Python
        # controller/worker, FastAPI, and finally the official Copilot Runtime.
        for name in ("washer", "ac", "bulb"):
            spawn(name, [sys.executable, "-m", "smart_home_energy." + name])
        spawn("matter-bridge", ["node", str(ROOT / "matter" / "bridge.mjs")], ROOT / "matter")
        spawn("control", [sys.executable, "-m", "smart_home_energy.control"])
        spawn("api", [sys.executable, "-m", "smart_home_energy.api", "--port", str(api_port)])
        wait(lambda: healthy(api, "/api/home"), "fastapi_ready", 25)
        spawn("copilot-runtime", ["node", str(ROOT / "web" / "node_modules" / "tsx" / "dist" / "cli.mjs"),
                                   str(ROOT / "web" / "runtime" / "server.ts")], ROOT / "web")
        wait(lambda: healthy(runtime, "/api/copilotkit/info") and healthy(api, "/api/copilotkit/info"),
             "runtime_discovery_and_same_origin_proxy", 35)
        info = client.get(runtime + "/api/copilotkit/info").json()
        agents = info.get("agents", [])
        assert "home" in (agents.keys() if isinstance(agents, dict) else agents), info
        wait(lambda: all(item["fresh"] for item in client.get(api + "/api/home").json()["devices"].values()),
             "matter_device_feedback", 90)

        prompt = ("請分別為冷氣和洗衣提出各自的計畫：冷氣現在開始，目標 27°C，維持約一小時；"
                  "洗衣請在接下來 24 小時內安排一次標準洗程，遵守安靜時段。只建立待核准計畫，"
                  "不要核准、不要啟動設備，也不要呼叫任何設備控制。")
        message_id = run_id + ":message"
        payload = {"threadId": "home", "runId": run_id,
                   "messages": [{"id": message_id, "role": "user", "content": prompt}],
                   "state": {}, "tools": [], "context": []}
        with client.stream("POST", api + "/api/copilotkit/agent/home/run", json=payload,
                           headers={"Accept": "text/event-stream"}, timeout=httpx.Timeout(180, connect=5)) as response:
            assert response.status_code == 200, response.read().decode("utf-8", "replace")
            terminal = None
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event.get("type") in {"RUN_FINISHED", "RUN_ERROR"}:
                    terminal = event
                    break
            assert terminal and terminal.get("type") == "RUN_FINISHED", terminal
        checks["live_model_run_through_copilot_runtime"] = True

        state = wait(lambda: (lambda value: value if any(r["id"] == run_id and r["status"] in {"completed", "partial_success"}
                                                       for r in value["runs"]) else None)(client.get(api + "/api/assistant").json()),
                     "python_agent_persisted_run", 20)
        cards = [tool for message in state["messages"] for tool in message.get("toolCalls", [])
                 if tool.get("function", {}).get("name") == "render_task_card"]
        assert len(cards) == 2, f"expected separate AC/washer approval cards, got {len(cards)}"
        tasks = {task["device_id"]: task for task in state["tasks"] if task["device_id"] in {"ac-1", "washer-1"}}
        assert set(tasks) == {"ac-1", "washer-1"}, tasks
        assert all(task["status"] in {"pending", "needs_details"} for task in tasks.values()), tasks
        assert all(task.get("plan") or task.get("ac_plan") for task in tasks.values()), tasks
        assert not repo.records("commands"), "the model must not emit appliance commands before approval"
        logical_now = datetime.fromisoformat(client.get(api + "/api/home").json()["clock"]["now"])
        checks["two_saved_approval_cards_without_device_commands"] = True

        # Model plans optimize the configured time-of-use tariff and may be
        # hours away. Exercise the UI's versioned edit API to bring them into
        # the accelerated test window; never write task/device state directly.
        start = logical_now + timedelta(minutes=5)
        for device_id in ("ac-1", "washer-1"):
            task = tasks[device_id]
            assert task["status"] == "pending", task
            if device_id == "ac-1":
                constraints = {**task["ac_constraints"], "start": start.isoformat(),
                               "end": (start + timedelta(hours=1)).isoformat()}
                post_request("schedule_ac_form", {"task_id": task["task_id"], "expected_version": task["version"],
                                                   "device_id": device_id, "constraints": constraints})
            else:
                constraints = {**task["constraints"], "earliest": start.isoformat(),
                               "deadline": (start + timedelta(minutes=30)).isoformat()}
                post_request("schedule_form", {"task_id": task["task_id"], "expected_version": task["version"],
                                                "device_id": device_id, "constraints": constraints})
            task_response = client.get(api + "/api/tasks/" + task["task_id"], timeout=5)
            task_response.raise_for_status()
            tasks[device_id] = task_response.json()
            assert tasks[device_id]["version"] > task["version"]
        checks["versioned_plan_edits_accepted"] = True

        # Exercise the exact versioned approval path exposed by the React cards.
        for device_id in ("ac-1", "washer-1"):
            task = tasks[device_id]
            assert task["status"] == "pending", task
            approval = {"task_id": task["task_id"], "expected_version": task["version"]}
            if device_id == "washer-1":
                approval["max_cost"] = task["plan"]["cost"]
            post_request("approve", approval)
        checks["both_independent_approvals_accepted"] = True

        home = wait(lambda: (lambda value: value if all(
            value["devices"][device]["active_task"] and value["devices"][device]["active_task"]["status"] == "running"
            for device in ("ac-1", "washer-1")) else None)(client.get(api + "/api/home").json()),
            "both_appliances_running_over_matter", 35)
        run_ids = {device: home["devices"][device]["state"]["run_id"] for device in ("ac-1", "washer-1")}
        assert all(run_ids.values()), run_ids
        wait(lambda: (lambda value: value if all(value["devices"][device]["latest_task"]
                 and value["devices"][device]["latest_task"]["status"] == "completed"
                 for device in ("ac-1", "washer-1")) else None)(client.get(api + "/api/home").json()),
             "both_approved_appliances_completed", 100)
        for device, expected_run in run_ids.items():
            snapshot = client.get(api + "/api/home").json()["devices"][device]["state"]
            assert snapshot["run_id"] == expected_run, (device, snapshot)
        checks["matter_feedback_reflected_in_react_home_api"] = True

        completed = client.get(api + "/api/home").json()
        agent_runs = [json.loads(row["data"]) for row in repo.records("agent_runs", 100)]
        providers = sorted({row["provider"] for row in agent_runs if row.get("provider")})
        summary = {"passed": True, "transport": "matter", "model_providers": providers,
                   "copilot_run_id": run_id, "tasks": {device: {
                   "proposal_status": tasks[device]["status"],
                   "final_status": completed["devices"][device]["latest_task"]["status"],
                   "task_id": tasks[device]["task_id"], "run_id": run_ids[device]} for device in tasks},
                   "checks": checks, "directory": str(directory)}
        (ROOT / "artifacts" / "copilotkit-live-smoke.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        client.close()
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()

"""JSON-lines IPC to a real Matter controller, with no direct MQTT fallback."""
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from queue import Queue, Empty, Full
import subprocess
import threading
import time
from uuid import uuid4

from smart_home_energy.domain import decode, encode
from smart_home_energy.washer import DeviceState, CommandResult

logger = logging.getLogger(__name__)


class MatterHub:
    def __init__(self, repo, settings, hems):
        self.repo, self.settings, self.hems = repo, settings, hems
        self.process = None
        self.health, self.queues = {}, {d: Queue(maxsize=1000) for d in ("washer-1", "ac-1")}
        self.lock, self.users = threading.RLock(), 0
        self.thread = None
        self.log = None

    def start(self):
        with self.lock:
            self.users += 1
            if self.process is not None:
                return
            root = Path(__file__).resolve().parents[2]
            env = os.environ.copy()
            env.update(MQTT_HOST=self.settings.mqtt_host, MQTT_PORT=str(self.settings.mqtt_port), HEMS_MQTT_ROOT=self.hems.mqtt_root)
            env.setdefault("HEMS_MATTER_STORAGE", str(root / "artifacts/matter"))
            logpath = Path(self.hems.database).parent / "matter-controller.log"
            logpath.parent.mkdir(parents=True, exist_ok=True)
            self.log = logpath.open("a", encoding="utf-8")
            self.process = subprocess.Popen([env.get("NODE_EXE", "node"), str(root / "matter/controller.mjs")], cwd=root / "matter", env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.thread = threading.Thread(target=self._read, daemon=True, name="matter-feedback")
            self.thread.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                value = json.loads(line)
                device = value.get("device_id")
                if device not in self.queues:
                    continue
                if value.get("kind") == "health":
                    self.health[device] = (bool(value["connected"]), time.monotonic())
                elif value.get("kind") in {"state", "result"}:
                    kind = value["kind"]
                    if device == "ac-1":
                        from smart_home_energy.ac import ACState
                        cls = ACState if kind == "state" else CommandResult
                    else:
                        cls = DeviceState if kind == "state" else CommandResult
                    item = decode(cls, value["item"])
                    if item.device_id != device:
                        raise ValueError("Matter 回饋設備不符")
                    self.queues[device].put_nowait((kind, item))
                elif value.get("ok") is False:
                    self.health[device] = (False, time.monotonic())
                    logger.warning("Matter command/read rejected: %s", value.get("error"))
            except (ValueError, TypeError, KeyError, Full) as error:
                logger.warning("Ignored Matter feedback: %s", error)
        for device in self.queues:
            self.health[device] = (False, time.monotonic())

    def connected(self, device):
        connected, observed = self.health.get(device, (False, 0))
        return bool(self.process and self.process.poll() is None and connected and time.monotonic() - observed <= 10)

    def send(self, command):
        with self.lock:
            if not self.connected(command.device_id):
                return False
            try:
                self.process.stdin.write(json.dumps({"id": uuid4().hex, "op": "command", "command": encode(command)}, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                return True
            except (BrokenPipeError, OSError):
                self.health[command.device_id] = (False, time.monotonic())
                return False

    def stop(self):
        with self.lock:
            self.users = max(0, self.users - 1)
            if self.users or self.process is None:
                return
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.thread.join(timeout=2)
            self.process.stdout.close()
            self.log.close()
            self.process = None


class MatterTransport:
    def __init__(self, hub, device_id):
        self.hub, self.device_id = hub, device_id

    @property
    def connected(self):
        return self.hub.connected(self.device_id)

    def start(self):
        self.hub.start()

    def stop(self):
        self.hub.stop()

    def publish(self, kind, item):
        if kind != "command" or item.device_id != self.device_id:
            raise ValueError("Matter transport 只接受指定設備命令")
        return self.hub.send(item)

    def drain(self):
        while True:
            try:
                yield self.hub.queues[self.device_id].get_nowait()
            except Empty:
                return

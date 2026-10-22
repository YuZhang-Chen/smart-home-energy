"""Resident controller for immediate bulb actions, with feedback-based completion."""

from datetime import datetime, timedelta
import json
import sqlite3
from uuid import uuid4

from smart_home_energy.bulb_domain import BulbCommand, BulbState, kelvin_to_mired, percent_to_level
from smart_home_energy.domain import decode, utcnow


def fresh(state: BulbState | None, real: datetime) -> bool:
    return state is not None and -2 <= (real - state.observed_at).total_seconds() <= 10


class BulbController:
    def __init__(self, repo, transport):
        self.repo, self.transport = repo, transport

    def state(self) -> BulbState | None:
        data = self.repo.device("bulb-1")
        return decode(BulbState, data) if data else None

    def finish(self, action: dict, status: str, message: str):
        self.repo.save_bulb_action(action["request_id"], status, action["data"])
        self.repo.finish_request(action["request_id"], "done" if status == "confirmed" else "error", message)

    def ingest(self, real: datetime):
        for kind, item in self.transport.drain():
            if item.device_id != "bulb-1":
                continue
            if kind == "state":
                before = self.state()
                if item.observed_at > real + timedelta(seconds=2):
                    continue
                if before and ((item.boot_id == before.boot_id and item.sequence <= before.sequence)
                               or (item.boot_id != before.boot_id and item.observed_at <= before.observed_at)):
                    continue
                self.repo.save_device(item)
            elif kind == "result":
                self.repo.command_result(item)
                action = self.repo.bulb_action(item.action_id)
                if action and action["data"]["command_id"] == item.command_id:
                    if not item.accepted and action["status"] in {"dispatched", "accepted"}:
                        self.finish(action, "rejected", item.message)
                    elif action["status"] == "dispatched":
                        self.repo.save_bulb_action(item.action_id, "accepted", action["data"])

    def reconcile(self, real: datetime):
        action = self.repo.active_bulb_action()
        if not action:
            return
        target = action["data"]
        state = self.state()
        if state and fresh(state, real) and state.action_id == action["request_id"] and not state.transitioning:
            if (state.power == target["power"] and state.level == target["level"]
                    and state.mired == target["mired"] and state.last_command_id == target["command_id"]):
                self.finish(action, "confirmed", "燈泡已達到設定並回報狀態")
                return
        if datetime.fromisoformat(target["confirm_by"]) <= real:
            self.finish(action, "unknown", "燈泡狀態未確認，請查核設備")

    def process_requests(self, real: datetime):
        for request in self.repo.pending_requests():
            if request["kind"] != "set_bulb" or self.repo.bulb_action(request["id"]):
                continue
            if self.repo.active_bulb_action():
                return  # Serialize changes until the previous target is reconciled.
            try:
                with self.repo.transaction():
                    payload = json.loads(request["payload"])
                    state = self.state()
                    if not fresh(state, real) or not self.transport.connected:
                        raise ValueError("燈泡或傳輸尚未提供新鮮狀態")
                    brightness = payload.get("brightness_percent")
                    power = payload.get("power")
                    if brightness == 0:
                        if power is True:
                            raise ValueError("0% 亮度與開燈互相矛盾")
                        power = False
                    elif brightness is not None and power is None:
                        power = True
                    level = percent_to_level(brightness) if brightness not in (None, 0) else None
                    mired = kelvin_to_mired(payload["kelvin"]) if payload.get("kelvin") is not None else None
                    desired_power = power if power is not None else state.power
                    desired_level = level if level is not None else state.level
                    desired_mired = mired if mired is not None else state.mired
                    duration = payload.get("duration_seconds", 0.5)
                    command = BulbCommand(uuid4().hex, request["id"], "set", state.boot_id,
                                          state.state_version, state.generation + 1,
                                          real + timedelta(seconds=10), "request:" + request["id"],
                                          power=power, level=level, mired=mired, duration_seconds=duration)
                    target = {"command_id": command.command_id, "power": desired_power, "level": desired_level,
                              "mired": desired_mired, "confirm_by": (real + timedelta(seconds=duration + 6)).isoformat()}
                    self.repo.save_command(command)
                    self.repo.save_bulb_action(request["id"], "dispatched", target)
                    if not self.transport.publish("command", command):
                        raise ValueError("燈泡命令未能送出")
            except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as error:
                self.repo.finish_request(request["id"], "error", str(error))
                if self.repo.bulb_action(request["id"]):
                    action = self.repo.bulb_action(request["id"])
                    self.repo.save_bulb_action(request["id"], "unknown", action["data"])

    def tick(self, real: datetime | None = None):
        real = real or utcnow()
        self.ingest(real)
        self.reconcile(real)
        self.process_requests(real)

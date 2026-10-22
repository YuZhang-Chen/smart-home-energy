"""Persistent virtual bulb; its reported state is the source of truth."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
import os
import time
from uuid import uuid4

from smart_home_energy.bulb_domain import BulbCommand, BulbResult, BulbState
from smart_home_energy.config import Settings, HemsSettings
from smart_home_energy.domain import decode, encode, utcnow
from smart_home_energy.repository import Repository
from smart_home_energy.service_lock import service_lock
from smart_home_energy.washer import WasherTransport


@dataclass(frozen=True)
class BulbTransition:
    start: datetime
    end: datetime
    from_level: int
    to_level: int
    from_mired: int
    to_mired: int
    final_power: bool
    stored_level: int


class VirtualBulb:
    def __init__(self, repo: Repository, real: datetime | None = None):
        self.repo = repo
        real = real or utcnow()
        previous = repo.meta("bulb_state")
        unfinished = repo.meta("bulb_transition")
        if previous:
            state = decode(BulbState, previous)
            self.state = replace(state, boot_id=uuid4().hex, sequence=0, observed_at=real,
                                 transitioning=False, fault="漸變因設備重啟中斷" if unfinished else state.fault)
        else:
            self.state = BulbState(uuid4().hex, 0, real)
        self.transition: BulbTransition | None = None
        with repo.transaction():
            repo.set_meta("bulb_state", self.state)
            repo.set_meta("bulb_transition", None)

    def tick(self, real: datetime | None = None) -> BulbState:
        real = real or utcnow()
        with self.repo.transaction():
            state = replace(self.state, sequence=self.state.sequence + 1, observed_at=real)
            t = self.transition
            if t:
                span = (t.end - t.start).total_seconds()
                fraction = max(0, min(1, (real - t.start).total_seconds() / span))
                effective = round(t.from_level + (t.to_level - t.from_level) * fraction)
                mired = round(t.from_mired + (t.to_mired - t.from_mired) * fraction)
                state = replace(state, power=effective > 0, level=max(1, effective) if effective > 0 else state.level,
                                mired=mired, transitioning=fraction < 1)
                if fraction >= 1:
                    state = replace(state, power=t.final_power, level=t.stored_level, mired=t.to_mired)
                    self.transition = None
                    self.repo.set_meta("bulb_transition", None)
            self.state = state
            self.repo.set_meta("bulb_state", state)
            return state

    def handle(self, command: BulbCommand, real: datetime | None = None) -> BulbResult:
        real = real or utcnow()
        with self.repo.transaction():
            previous = self.repo.meta("bulb_command:" + command.command_id)
            if previous:
                if previous["command"] == encode(command):
                    return decode(BulbResult, previous["result"])
                return BulbResult(command.command_id, command.action_id, False,
                                  "命令識別碼已用於其他內容", self.state.boot_id, self.state.state_version)
            self.tick(real)
            try:
                if command.expires_at <= real or command.expected_boot != self.state.boot_id:
                    raise ValueError("命令過期或設備已重新啟動")
                if command.action == "set":
                    if command.expected_state_version != self.state.state_version or command.generation < self.state.generation:
                        raise ValueError("燈泡設定已更新，舊命令不可覆蓋")
                    target_level = command.level if command.level is not None else self.state.level
                    target_mired = command.mired if command.mired is not None else self.state.mired
                    target_power = command.power if command.power is not None else (True if command.level is not None else self.state.power)
                    start_level = self.state.level if self.state.power else 0
                    end_level = target_level if target_power else 0
                    if command.duration_seconds > 0 and (start_level != end_level or self.state.mired != target_mired):
                        self.transition = BulbTransition(real, real + timedelta(seconds=command.duration_seconds),
                                                          start_level, end_level, self.state.mired, target_mired,
                                                          target_power, target_level)
                        self.state = replace(self.state, transitioning=True)
                        self.repo.set_meta("bulb_transition", self.transition)
                    else:
                        self.transition = None
                        self.repo.set_meta("bulb_transition", None)
                        self.state = replace(self.state, power=target_power, level=target_level, mired=target_mired,
                                             transitioning=False)
                    self.state = replace(self.state, state_version=self.state.state_version + 1,
                                         generation=command.generation, last_command_id=command.command_id,
                                         action_id=command.action_id, fault=None)
                    self.repo.set_meta("bulb_state", self.state)
                result = BulbResult(command.command_id, command.action_id, True, "已受理；請以狀態確認",
                                    self.state.boot_id, self.state.state_version)
            except ValueError as error:
                result = BulbResult(command.command_id, command.action_id, False, str(error),
                                    self.state.boot_id, self.state.state_version)
            self.repo.set_meta("bulb_command:" + command.command_id, {"command": encode(command), "result": encode(result)})
            return result


def main():
    logging.basicConfig(level=logging.INFO)
    settings, hems = Settings.from_env(), HemsSettings.from_env()
    path = os.getenv("HEMS_BULB_DB", str(hems.device_database) + ".bulb")
    if os.path.abspath(path) in {hems.database, hems.device_database,
                                 os.getenv("HEMS_AC_DB", str(hems.device_database) + ".ac")}:
        raise ValueError("燈泡、冷氣、洗衣與控制資料庫須分開")
    with service_lock(path + ".lock"):
        bulb = VirtualBulb(Repository(path))
        transport = WasherTransport(settings, hems.mqtt_root, "device", "bulb-1")
        transport.start()
        try:
            while True:
                for _, command in transport.drain():
                    transport.publish("result", bulb.handle(command))
                transport.publish("state", bulb.tick())
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            transport.stop()


if __name__ == "__main__":
    main()

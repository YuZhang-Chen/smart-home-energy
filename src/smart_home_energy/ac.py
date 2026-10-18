"""Persistent controllable AC; a scheduled end is executed without an online LLM."""
from dataclasses import dataclass, replace
from datetime import datetime
import logging
import json
import math
import os
import time
from uuid import uuid4

from smart_home_energy.ac_domain import ACPlan, ACApproval, make_ac_plan, predict, validate_ac
from smart_home_energy.config import Settings, HemsSettings
from smart_home_energy.domain import decode, encode, utcnow, number
from smart_home_energy.repository import Repository
from smart_home_energy.washer import DeviceCommand, DeviceState, CommandResult, WasherTransport


@dataclass(frozen=True)
class ACCommand(DeviceCommand):
    plan: ACPlan | None = None
    approval: ACApproval | None = None


@dataclass(frozen=True)
class ACState(DeviceState):
    device_id: str = "ac-1"
    temperature_c: float = 30
    target_c: float = 27

    def __post_init__(self):
        super().__post_init__()
        number(self.temperature_c, "室溫", -50)
        number(self.target_c, "目標溫度", -50)


class VirtualAC:
    def __init__(self, repo: Repository, clock, real: datetime | None = None):
        self.repo, self.clock = repo, clock
        real = real or utcnow()
        self.boot_id = uuid4().hex
        old = repo.meta("ac_state")
        self.state = decode(ACState, old) if old else ACState(self.boot_id, 0, real, clock.now(real), clock.clock_id)
        self.state = replace(self.state, boot_id=self.boot_id, sequence=0, observed_at=real, logical_at=clock.now(real))
        if self.state.status == "running":
            self.state = replace(self.state, status="fault", power_w=0, fault="冷氣程序重啟；無法證明連續運轉，請人工解除")
        self.repo.set_meta("ac_state", self.state)

    def tick(self, real=None, clock=None):
        real = real or utcnow()
        with self.repo.transaction():
            if clock is not None and clock.clock_id != self.clock.clock_id:
                if self.state.status == "running":
                    self.state = replace(self.state, status="fault", fault="運轉中時鐘被更換", power_w=0)
                self.clock = clock
            logical = self.clock.now(real)
            old = self.state
            s = replace(old, sequence=old.sequence + 1, observed_at=real, logical_at=logical, clock_id=self.clock.clock_id)
            if s.status == "running":
                plan = decode(ACPlan, self.repo.meta("ac_execution"))
                c = plan.constraints
                elapsed = max(0, (logical - c.start).total_seconds())
                temperature, energy, cost = predict(c, plan.initial_c, plan.tariff, elapsed)
                duration = (c.end - c.start).total_seconds()
                done = elapsed >= duration
                if done:
                    temperature = c.ambient_c + (temperature - c.ambient_c) * math.exp(-(elapsed - duration) / c.warming_seconds)
                s = replace(s, temperature_c=temperature, energy_kwh=energy, cost=cost, progress=min(1, elapsed / duration),
                            status="completed" if done else "running", stage="已依計畫關機" if done else "降溫／舒適管理",
                            power_w=0 if done else min(1200, max(100, 100 + 300 * (temperature - c.target_c))))
            else:
                raw = self.repo.meta("ac_execution")
                ambient = raw["constraints"]["ambient_c"] if raw else 32
                tau = raw["constraints"]["warming_seconds"] if raw else 1800
                elapsed = max(0, (logical - old.logical_at).total_seconds())
                s = replace(s, temperature_c=ambient + (old.temperature_c - ambient) * math.exp(-elapsed / tau), power_w=0)
            self.state = s
            self.repo.set_meta("ac_state", s)
            return s

    def handle(self, command: ACCommand, real=None):
        real = real or utcnow()
        with self.repo.transaction():
            cached = self.repo.meta("result:" + command.command_id)
            if cached:
                return decode(CommandResult, cached)
            self.tick(real)
            try:
                if command.device_id != "ac-1" or command.expires_at <= real or command.clock_id != self.clock.clock_id or command.expected_boot != self.boot_id:
                    raise ValueError("命令設備、時鐘、啟動識別碼不符或已過期")
                if command.action == "start":
                    if self.state.status not in {"idle", "completed", "aborted"} or not command.plan or not command.approval or not command.task_id or not command.run_id or command.plan_version < 1:
                        raise ValueError("冷氣啟動缺少核准或狀態不符")
                    if self.repo.meta("started:" + command.task_id):
                        raise ValueError("同一任務不可重跑")
                    now = self.clock.now(real)
                    if now < command.plan.constraints.start or now >= command.plan.constraints.end:
                        raise ValueError("不在核准運轉時段")
                    c = replace(command.plan.constraints, start=now)
                    actual = make_ac_plan(c, self.state.temperature_c, command.plan.tariff, self.clock.clock_id)
                    validate_ac(actual, now, self.clock.clock_id, command.approval)
                    self.repo.set_meta("ac_execution", actual)
                    self.repo.set_meta("started:" + command.task_id, command.run_id)
                    self.state = replace(self.state, status="running", task_id=command.task_id, run_id=command.run_id,
                        plan_version=command.plan_version, target_c=c.target_c, energy_kwh=0, cost=0, progress=0,
                        stage="降溫／舒適管理", fault=None, power_w=min(1200, max(100, 100 + 300 * (self.state.temperature_c - c.target_c))))
                elif command.action == "stop":
                    if self.state.status != "running" or command.task_id != self.state.task_id or command.run_id != self.state.run_id:
                        raise ValueError("停止命令不符目前冷氣任務")
                    self.state = replace(self.state, status="aborted", stage="人工中止", power_w=0)
                elif command.action == "reset":
                    if self.state.status != "fault":
                        raise ValueError("僅故障狀態可解除")
                    self.state = replace(self.state, status="idle", fault=None, stage="待機", power_w=0)
                self.state = replace(self.state, last_command_id=command.command_id)
                result = CommandResult(command.command_id, True, "冷氣命令已受理，請以狀態確認", self.boot_id, "ac-1")
            except ValueError as error:
                result = CommandResult(command.command_id, False, str(error), self.boot_id, "ac-1")
            self.repo.set_meta("ac_state", self.state)
            self.repo.set_meta("result:" + command.command_id, result)
            return result


def main():
    from smart_home_energy.service_lock import service_lock
    logging.basicConfig(level=logging.INFO)
    settings, hems = Settings.from_env(), HemsSettings.from_env()
    path = os.getenv("HEMS_AC_DB", str(hems.device_database) + ".ac")
    if os.path.abspath(path) in {hems.database, hems.device_database}:
        raise ValueError("冷氣、洗衣及控制資料庫須分開")
    with service_lock(path + ".lock"):
        shared, device = Repository(hems.database), Repository(path)
        ac = VirtualAC(device, shared.clock())
        transport = WasherTransport(settings, hems.mqtt_root, "device", "ac-1")
        transport.start()
        try:
            while True:
                ac.tick(clock=shared.clock())
                for _, command in transport.drain():
                    transport.publish("result", ac.handle(command))
                transport.publish("state", ac.state)
                # The original monitor consumes the very same physical snapshot.
                s = ac.state
                transport.client.publish(settings.mqtt_topic, json.dumps({"run_id": s.boot_id, "sequence": s.sequence,
                    "timestamp": s.observed_at.isoformat(), "device_id": "ac-1", "temperature_c": s.temperature_c,
                    "power_w": s.power_w, "energy_kwh": s.energy_kwh}), retain=True)
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            transport.stop()


if __name__ == "__main__":
    main()

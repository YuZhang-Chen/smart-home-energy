"""Persistent controllable virtual AC; scheduled shutdown needs no online LLM."""
from dataclasses import dataclass, replace
from datetime import datetime
import json
import logging
import math
import os
import time
from uuid import uuid4

from smart_home_energy.ac_domain import (
    ACPlan,
    ACApproval,
    cooling_power_w,
    make_ac_plan,
    predict,
    validate_ac,
)
from smart_home_energy.config import Settings, HemsSettings
from smart_home_energy.domain import decode, utcnow, number
from smart_home_energy.repository import Repository
from smart_home_energy.washer import (
    DeviceCommand,
    DeviceState,
    CommandResult,
    WasherTransport,
)


DEFAULT_AMBIENT_C = 32.0
DEFAULT_WARMING_SECONDS = 1800.0


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


def _execution_constraint(raw, key: str, default):
    """Read a constraint from either a decoded dataclass or stored JSON dict."""
    if raw is None:
        return default

    constraints = (
        raw.get("constraints")
        if isinstance(raw, dict)
        else getattr(raw, "constraints", None)
    )
    if constraints is None:
        return default

    if isinstance(constraints, dict):
        return constraints.get(key, default)

    return getattr(constraints, key, default)


def _passive_temperature(
    current_c: float,
    ambient_c: float,
    elapsed_seconds: float,
    warming_seconds: float,
) -> float:
    """Simulate a switched-off room gradually returning to ambient temperature."""
    number(current_c, "目前室溫", -50)
    number(ambient_c, "環境溫度", -50)
    number(elapsed_seconds, "經過秒數", 0)
    number(warming_seconds, "回溫時間常數", 1)

    return ambient_c + (current_c - ambient_c) * math.exp(
        -elapsed_seconds / warming_seconds
    )


class VirtualAC:
    """A software-only AC device whose state is published through MQTT.

    This model is intended for the smart-home project demonstration.
    It does not communicate with a real air conditioner or IR blaster.
    """

    def __init__(self, repo: Repository, clock, real: datetime | None = None):
        self.repo, self.clock = repo, clock
        real = real or utcnow()
        self.boot_id = uuid4().hex

        old = repo.meta("ac_state")
        self.state = (
            decode(ACState, old)
            if old
            else ACState(
                self.boot_id,
                0,
                real,
                clock.now(real),
                clock.clock_id,
            )
        )

        self.state = replace(
            self.state,
            boot_id=self.boot_id,
            sequence=0,
            observed_at=real,
            logical_at=clock.now(real),
        )

        # A running process cannot safely resume an old command after restart.
        if self.state.status == "running":
            self.state = replace(
                self.state,
                status="fault",
                power_w=0,
                fault="冷氣程序重啟；無法證明連續運轉，請人工解除",
            )

        self.repo.set_meta("ac_state", self.state)

    def tick(self, real=None, clock=None):
        real = real or utcnow()

        with self.repo.transaction():
            if clock is not None and clock.clock_id != self.clock.clock_id:
                if self.state.status == "running":
                    self.state = replace(
                        self.state,
                        status="fault",
                        fault="運轉中時鐘被更換",
                        power_w=0,
                    )
                self.clock = clock

            logical = self.clock.now(real)
            old = self.state

            state = replace(
                old,
                sequence=old.sequence + 1,
                observed_at=real,
                logical_at=logical,
                clock_id=self.clock.clock_id,
            )

            if state.status == "running":
                plan = decode(ACPlan, self.repo.meta("ac_execution"))
                constraints = plan.constraints

                elapsed = max(
                    0.0,
                    (logical - constraints.start).total_seconds(),
                )
                temperature, energy, cost = predict(
                    constraints,
                    plan.initial_c,
                    plan.tariff,
                    elapsed,
                )

                duration = (
                    constraints.end - constraints.start
                ).total_seconds()
                done = elapsed >= duration

                if done:
                    # After scheduled shutdown, the room gradually returns
                    # toward the ambient temperature.
                    temperature = _passive_temperature(
                        temperature,
                        constraints.ambient_c,
                        elapsed - duration,
                        constraints.warming_seconds,
                    )

                if done:
                    stage = "已依計畫關機"
                    power_w = 0
                    status = "completed"
                elif temperature > constraints.target_c + 0.5:
                    stage = "智慧降溫中"
                    power_w = cooling_power_w(
                        temperature,
                        constraints.target_c,
                    )
                    status = "running"
                else:
                    # The first-order model approaches the setpoint without
                    # overshooting; minimum power represents low-load holding.
                    stage = "恆溫維持中"
                    power_w = cooling_power_w(
                        temperature,
                        constraints.target_c,
                    )
                    status = "running"

                state = replace(
                    state,
                    temperature_c=temperature,
                    target_c=constraints.target_c,
                    energy_kwh=energy,
                    cost=cost,
                    progress=min(1.0, elapsed / duration),
                    status=status,
                    stage=stage,
                    power_w=power_w,
                )

            else:
                raw_execution = self.repo.meta("ac_execution")
                ambient = _execution_constraint(
                    raw_execution,
                    "ambient_c",
                    DEFAULT_AMBIENT_C,
                )
                warming_seconds = _execution_constraint(
                    raw_execution,
                    "warming_seconds",
                    DEFAULT_WARMING_SECONDS,
                )
                elapsed = max(
                    0.0,
                    (logical - old.logical_at).total_seconds(),
                )

                state = replace(
                    state,
                    temperature_c=_passive_temperature(
                        old.temperature_c,
                        ambient,
                        elapsed,
                        warming_seconds,
                    ),
                    power_w=0,
                )

            self.state = state
            self.repo.set_meta("ac_state", state)
            return state

    def handle(self, command: ACCommand, real=None):
        real = real or utcnow()

        with self.repo.transaction():
            cached = self.repo.meta("result:" + command.command_id)
            if cached:
                return decode(CommandResult, cached)

            # Refresh the device state before validating a new command.
            self.tick(real)

            try:
                if (
                    command.device_id != "ac-1"
                    or command.expires_at <= real
                    or command.clock_id != self.clock.clock_id
                    or command.expected_boot != self.boot_id
                ):
                    raise ValueError(
                        "命令設備、時鐘、啟動識別碼不符或已過期"
                    )

                if command.action == "start":
                    if (
                        self.state.status
                        not in {"idle", "completed", "aborted"}
                        or not command.plan
                        or not command.approval
                        or not command.task_id
                        or not command.run_id
                        or command.plan_version < 1
                    ):
                        raise ValueError("冷氣啟動缺少核准或狀態不符")

                    if self.repo.meta("started:" + command.task_id):
                        raise ValueError("同一任務不可重跑")

                    now = self.clock.now(real)
                    if (
                        now < command.plan.constraints.start
                        or now >= command.plan.constraints.end
                    ):
                        raise ValueError("不在核准運轉時段")

                    # Start the approved plan from the actual current room
                    # temperature, not from an earlier forecast snapshot.
                    constraints = replace(
                        command.plan.constraints,
                        start=now,
                    )
                    actual_plan = make_ac_plan(
                        constraints,
                        self.state.temperature_c,
                        command.plan.tariff,
                        self.clock.clock_id,
                    )
                    validate_ac(
                        actual_plan,
                        now,
                        self.clock.clock_id,
                        command.approval,
                    )

                    self.repo.set_meta("ac_execution", actual_plan)
                    self.repo.set_meta(
                        "started:" + command.task_id,
                        command.run_id,
                    )

                    self.state = replace(
                        self.state,
                        status="running",
                        task_id=command.task_id,
                        run_id=command.run_id,
                        plan_version=command.plan_version,
                        target_c=constraints.target_c,
                        energy_kwh=0,
                        cost=0,
                        progress=0,
                        stage="智慧降溫中",
                        fault=None,
                        power_w=cooling_power_w(
                            self.state.temperature_c,
                            constraints.target_c,
                        ),
                    )

                elif command.action == "stop":
                    if (
                        self.state.status != "running"
                        or command.task_id != self.state.task_id
                        or command.run_id != self.state.run_id
                    ):
                        raise ValueError("停止命令不符目前冷氣任務")

                    self.state = replace(
                        self.state,
                        status="aborted",
                        stage="人工中止",
                        power_w=0,
                    )

                elif command.action == "reset":
                    if self.state.status != "fault":
                        raise ValueError("僅故障狀態可解除")

                    self.state = replace(
                        self.state,
                        status="idle",
                        fault=None,
                        stage="待機",
                        power_w=0,
                    )
                else:
                    raise ValueError("不支援的冷氣命令")

                self.state = replace(
                    self.state,
                    last_command_id=command.command_id,
                )
                result = CommandResult(
                    command.command_id,
                    True,
                    "冷氣命令已受理，請以狀態確認",
                    self.boot_id,
                    "ac-1",
                )

            except ValueError as error:
                result = CommandResult(
                    command.command_id,
                    False,
                    str(error),
                    self.boot_id,
                    "ac-1",
                )

            self.repo.set_meta("ac_state", self.state)
            self.repo.set_meta(
                "result:" + command.command_id,
                result,
            )
            return result


def main():
    from smart_home_energy.service_lock import service_lock

    logging.basicConfig(level=logging.INFO)
    settings, hems = Settings.from_env(), HemsSettings.from_env()
    path = os.getenv(
        "HEMS_AC_DB",
        str(hems.device_database) + ".ac",
    )

    if os.path.abspath(path) in {hems.database, hems.device_database}:
        raise ValueError("冷氣、洗衣及控制資料庫須分開")

    with service_lock(path + ".lock"):
        shared = Repository(hems.database)
        device = Repository(path)
        ac = VirtualAC(device, shared.clock())
        transport = WasherTransport(
            settings,
            hems.mqtt_root,
            "device",
            "ac-1",
        )
        transport.start()

        try:
            while True:
                ac.tick(clock=shared.clock())

                for _, command in transport.drain():
                    transport.publish("result", ac.handle(command))

                transport.publish("state", ac.state)

                # The monitor receives the same virtual-device snapshot.
                state = ac.state
                transport.client.publish(
                    settings.mqtt_topic,
                    json.dumps(
                        {
                            "run_id": state.boot_id,
                            "sequence": state.sequence,
                            "timestamp": state.observed_at.isoformat(),
                            "device_id": "ac-1",
                            "temperature_c": state.temperature_c,
                            "power_w": state.power_w,
                            "energy_kwh": state.energy_kwh,
                        }
                    ),
                    retain=True,
                )
                time.sleep(1)

        except KeyboardInterrupt:
            pass
        finally:
            transport.stop()


if __name__ == "__main__":
    main()

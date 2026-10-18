"""Persistent virtual appliance and versioned MQTT protocol."""

from dataclasses import dataclass, replace, fields
from datetime import datetime, timedelta
import json
import logging
from queue import Queue, Empty, Full
import time
from uuid import uuid4

import paho.mqtt.client as mqtt

from smart_home_energy.config import Settings, HemsSettings
from smart_home_energy.domain import (
    ApprovalEnvelope, LogicalClock, Plan, decode, encode, integrate, make_plan,
    number, utcnow, aware, validate_plan,
)
from smart_home_energy.repository import Repository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceCommand:
    command_id: str
    device_id: str
    action: str
    clock_id: str
    expires_at: datetime
    expected_boot: str
    task_id: str | None = None
    plan_version: int = 0
    run_id: str | None = None
    plan: Plan | None = None
    approval: ApprovalEnvelope | None = None
    protocol_version: int = 1

    def __post_init__(self):
        aware(self.expires_at)
        if self.protocol_version != 1 or self.device_id not in {"washer-1", "ac-1"} or self.action not in {"start", "stop", "query", "reset"}:
            raise ValueError("不支援的命令協議、設備或動作")
        if not self.command_id or not self.clock_id or not self.expected_boot or type(self.plan_version) is not int or self.plan_version < 0:
            raise ValueError("命令識別碼／版本不合法")


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    accepted: bool
    message: str
    boot_id: str
    device_id: str = "washer-1"
    protocol_version: int = 1

    def __post_init__(self):
        if self.protocol_version != 1 or self.device_id not in {"washer-1", "ac-1"} or type(self.accepted) is not bool:
            raise ValueError("不支援的結果訊息")


@dataclass(frozen=True)
class DeviceState:
    boot_id: str
    sequence: int
    observed_at: datetime
    logical_at: datetime
    clock_id: str
    status: str = "idle"
    task_id: str | None = None
    plan_version: int = 0
    run_id: str | None = None
    stage: str = "待機"
    progress: float = 0
    power_w: float = 0
    energy_kwh: float = 0
    cost: float = 0
    fault: str | None = None
    last_command_id: str | None = None
    device_id: str = "washer-1"
    protocol_version: int = 1

    def __post_init__(self):
        aware(self.observed_at)
        aware(self.logical_at)
        if self.protocol_version != 1 or self.device_id not in {"washer-1", "ac-1"} or not self.boot_id or not self.clock_id:
            raise ValueError("不支援的狀態協議或設備")
        if type(self.sequence) is not int or self.sequence < 0 or type(self.plan_version) is not int or self.plan_version < 0:
            raise ValueError("狀態序號／版本不合法")
        if self.status not in {"idle", "running", "completed", "aborted", "fault"}:
            raise ValueError("不支援的設備狀態")
        for name in ("progress", "power_w", "energy_kwh", "cost"):
            number(getattr(self, name), name)
        if self.progress > 1:
            raise ValueError("進度不可超過 1")


class Washer:
    def __init__(self, device_repo: Repository, clock: LogicalClock, real: datetime | None = None):
        self.repo, self.clock = device_repo, clock
        self.boot_id = uuid4().hex
        real = real or utcnow()
        previous = self.repo.meta("washer_state")
        if previous:
            old = decode(DeviceState, previous)
            self.state = replace(old, boot_id=self.boot_id, sequence=0, observed_at=real,
                                 logical_at=clock.now(real), clock_id=clock.clock_id)
            if old.status == "running":
                self.state = replace(self.state, status="fault", power_w=0,
                                     fault="設備程序重啟，無法證明離線期間行程連續；請人工解除故障")
        else:
            self.state = DeviceState(self.boot_id, 0, real, clock.now(real), clock.clock_id)
        self.repo.set_meta("washer_state", self.state)

    def tick(self, real: datetime | None = None, clock: LogicalClock | None = None) -> DeviceState:
        real = real or utcnow()
        with self.repo.transaction():
            if clock is not None and clock.clock_id != self.clock.clock_id:
                if self.state.status == "running":
                    self.state = replace(self.state, status="fault", fault="行程中時鐘被更換", power_w=0)
                self.clock = clock
            state = replace(self.state, sequence=self.state.sequence + 1, observed_at=real,
                            logical_at=self.clock.now(real), clock_id=self.clock.clock_id)
            if state.status == "running":
                plan = decode(Plan, self.repo.meta("execution_plan"))
                elapsed = max(0, (state.logical_at - plan.start).total_seconds())
                energy, cost, stage, power = integrate(plan.program, plan.start, elapsed, plan.tariff)
                state = replace(state, energy_kwh=energy, cost=cost, stage=stage, power_w=power,
                                progress=min(1, elapsed / plan.program.seconds),
                                status="completed" if elapsed >= plan.program.seconds else "running")
            self.state = state
            self.repo.set_meta("washer_state", state)
            return state

    def handle(self, command: DeviceCommand, real: datetime | None = None) -> CommandResult:
        real = real or utcnow()
        with self.repo.transaction():
            previous = self.repo.meta("result:" + command.command_id)
            if previous:
                return decode(CommandResult, previous)
            self.tick(real)
            try:
                if command.device_id != "washer-1":
                    raise ValueError("設備識別碼不符")
                if command.expires_at <= real:
                    raise ValueError("命令已過期")
                if command.clock_id != self.clock.clock_id or command.expected_boot != self.boot_id:
                    raise ValueError("命令時鐘或設備啟動識別碼不符")
                if command.action == "start":
                    if self.state.status not in {"idle", "completed", "aborted"} or not command.task_id or not command.run_id or command.plan_version < 1:
                        raise ValueError("設備狀態不相容或缺少行程識別碼")
                    if self.repo.meta("started:" + command.task_id):
                        raise ValueError("同一任務已啟動過，不可重跑")
                    if command.plan is None or command.approval is None:
                        raise ValueError("缺少核准快照")
                    logical = self.clock.now(real)
                    if logical < command.plan.start:
                        raise ValueError("尚未到核准開始時間")
                    actual = make_plan(logical, command.plan.program, command.plan.tariff, self.clock.clock_id)
                    validate_plan(actual, command.approval.constraints, logical, self.clock.clock_id, command.approval)
                    self.repo.set_meta("execution_plan", actual)
                    self.repo.set_meta("started:" + command.task_id, command.run_id)
                    self.state = DeviceState(self.boot_id, self.state.sequence + 1, real, logical,
                        self.clock.clock_id, "running", command.task_id, command.plan_version, command.run_id,
                        stage=actual.program.stages[0].name, power_w=actual.program.stages[0].power_w,
                        last_command_id=command.command_id)
                elif command.action == "stop":
                    if self.state.status != "running" or command.task_id != self.state.task_id or command.run_id != self.state.run_id:
                        raise ValueError("停止命令不符目前行程")
                    self.state = replace(self.state, status="aborted", power_w=0, stage="已中止", last_command_id=command.command_id)
                elif command.action == "reset":
                    if self.state.status != "fault":
                        raise ValueError("僅故障狀態可人工解除")
                    self.state = replace(self.state, status="idle", fault=None, power_w=0, stage="待機", last_command_id=command.command_id)
                result = CommandResult(command.command_id, True, "命令已受理；完成情況請讀取狀態", self.boot_id)
            except ValueError as error:
                result = CommandResult(command.command_id, False, str(error), self.boot_id)
            # Result and appliance state are committed together before publishing either.
            self.repo.set_meta("washer_state", self.state)
            self.repo.set_meta("result:" + command.command_id, result)
            return result


class WasherTransport:
    """Callbacks only validate and enqueue messages. No model calls or control logic."""
    def __init__(self, settings: Settings, root: str, role: str, device_id: str = "washer-1"):
        self.device_id = device_id
        if role not in {"device", "control"}:
            raise ValueError("未知 MQTT 角色")
        self.root, self.role = root, role
        self.settings = settings
        self.messages: Queue = Queue(maxsize=1000)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"hems-{role}-{uuid4().hex[:12]}")
        self.client.reconnect_delay_set(1, 10)
        self.client.max_queued_messages_set(100)
        self.client.on_connect = self._connect
        self.client.on_message = self._message

    @property
    def connected(self) -> bool:
        return self.client.is_connected()

    def _connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            topics = ["command"] if self.role == "device" else ["state", "result"]
            for topic in topics:
                client.subscribe(f"{self.root}/devices/{self.device_id}/{topic}", qos=0 if topic == "state" else 1)

    def _message(self, client, userdata, message):
        kind = message.topic.rsplit("/", 1)[-1]
        try:
            if len(message.payload) > 65536 or (kind == "command" and message.retain):
                raise ValueError("訊息過大或 retained 命令")
            cls = {"command": DeviceCommand, "state": DeviceState, "result": CommandResult}[kind]
            if self.device_id == "ac-1":
                from smart_home_energy.ac import ACCommand, ACState
                cls = {"command": ACCommand, "state": ACState, "result": CommandResult}[kind]
            data = json.loads(message.payload)
            if not isinstance(data, dict) or set(data) != {f.name for f in fields(cls)}:
                raise ValueError("MQTT 訊息必須包含完整協議欄位")
            item = decode(cls, data)
            if item.device_id != self.device_id:
                raise ValueError("設備識別碼不符")
            self.messages.put_nowait((kind, item))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
            logger.warning("Ignored invalid washer message: %s", error)
        except Full:
            logger.error("Washer inbox full; waiting for next state/query")

    def publish(self, kind: str, item) -> bool:
        if not self.connected:
            return False
        if item.device_id != self.device_id:
            raise ValueError("設備識別碼不符")
        result = self.client.publish(f"{self.root}/devices/{self.device_id}/{kind}", json.dumps(encode(item), allow_nan=False),
                                     qos=0 if kind == "state" else 1, retain=kind == "state")
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def drain(self):
        while True:
            try:
                yield self.messages.get_nowait()
            except Empty:
                return

    def start(self):
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, 10)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()


def main():
    from smart_home_energy.service_lock import service_lock
    logging.basicConfig(level=logging.INFO)
    settings, hems = Settings.from_env(), HemsSettings.from_env()
    with service_lock(hems.device_database + ".lock"):
        shared, device = Repository(hems.database), Repository(hems.device_database)
        washer = Washer(device, shared.clock())
        transport = WasherTransport(settings, hems.mqtt_root, "device")
        transport.start()
        try:
            while True:
                washer.tick(clock=shared.clock())
                for _, command in transport.drain():
                    transport.publish("result", washer.handle(command))
                transport.publish("state", washer.state)
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            transport.stop()


if __name__ == "__main__":
    main()

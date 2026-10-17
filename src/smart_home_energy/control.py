"""Resident controller: UI requests, approval enforcement and appliance reconciliation."""

from datetime import datetime, timedelta
import json
import logging
import sqlite3
import threading
import time
from uuid import uuid4

from smart_home_energy.config import Settings, HemsSettings
from smart_home_energy.domain import (
    TaskConstraints, decode, encode, find_schedule, make_plan, utcnow, validate_plan,
)
from smart_home_energy.repository import Repository
from smart_home_energy.washer import DeviceCommand, DeviceState, WasherTransport

logger = logging.getLogger(__name__)


def fresh(state: DeviceState | None, real: datetime) -> bool:
    return state is not None and -2 <= (real - state.observed_at).total_seconds() <= 10


class Controller:
    def __init__(self, repo: Repository, transport, hems: HemsSettings, real: datetime | None = None,
                 monotonic=time.monotonic):
        self.repo, self.transport, self.hems = repo, transport, hems
        self.started_at = real or utcnow()
        self.monotonic = monotonic
        self.waits: dict[str, tuple[float, DeviceCommand]] = {}
        self.last_query = float("-inf")
        self.recovered = False

    def state(self) -> DeviceState | None:
        data = self.repo.device()
        return decode(DeviceState, data) if data else None

    def send(self, action: str, task=None, real: datetime | None = None) -> DeviceCommand:
        real = real or utcnow()
        state = self.state()
        if state is None or not self.transport.connected:
            raise ValueError("設備或 Broker 尚未連線")
        clock = self.repo.clock()
        command = DeviceCommand(uuid4().hex, "washer-1", action, clock.clock_id,
            real + timedelta(seconds=5), state.boot_id, task.task_id if task else None,
            task.version if task else 0, task.run_id if task else None,
            task.plan if task and action == "start" else None,
            task.approval if task and action == "start" else None)
        self.repo.save_command(command)
        # Persist before publish. A crash in this gap leads to reconciliation, never replay.
        self.waits[command.command_id] = (self.monotonic(), command)
        self.transport.publish("command", command)
        return command

    def query(self, real: datetime):
        if self.monotonic() - self.last_query >= 5 and self.state() and self.transport.connected:
            self.last_query = self.monotonic()
            self.send("query", real=real)

    def ingest(self, real: datetime):
        for kind, item in self.transport.drain():
            if kind == "state":
                previous = self.state()
                if item.observed_at > real + timedelta(seconds=2):
                    continue
                if previous:
                    if item.boot_id == previous.boot_id and item.sequence <= previous.sequence:
                        continue
                    if item.boot_id != previous.boot_id and item.observed_at <= previous.observed_at:
                        continue
                self.repo.save_device(item)
                if item.last_command_id in self.waits:
                    del self.waits[item.last_command_id]
            elif kind == "result":
                self.repo.command_result(item)
                wait = self.waits.get(item.command_id)
                if wait and (not item.accepted or wait[1].action == "query"):
                    self.waits.pop(item.command_id, None)
                if wait and not item.accepted:
                    self.repo.audit("command_rejected", item)
                    task = self.repo.active_task()
                    if task and wait[1].task_id == task.task_id and wait[1].action in {"start", "stop"}:
                        self.repo.event("rejected:" + task.task_id, task.task_id, {"reason": item.message})
                # Acceptance is not completion, and does not change task status.

    def process_requests(self, real: datetime):
        for request in self.repo.pending_requests():
            try:
                with self.repo.transaction():
                    payload = json.loads(request["payload"])
                    result = self.handle_request(request["kind"], payload, real)
                    self.repo.finish_request(request["id"], "done", result)
            except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as error:
                self.repo.finish_request(request["id"], "error", str(error))

    def handle_request(self, kind: str, p: dict, real: datetime) -> str:
        clock = self.repo.clock()
        if kind == "clock":
            self.repo.switch_clock(p["speed"])
            self.recovered = False
            return "時間模式已切換"
        if kind in {"create_form", "create_text"}:
            c = decode(TaskConstraints, p["constraints"]) if kind == "create_form" else None
            task = self.repo.create_task(c, self.hems.program(), self.hems.tariff(), p.get("text", ""))
            if c:
                plan = find_schedule(c, task.program, task.tariff, clock.now(real), clock.clock_id)
                self.repo.propose(task.task_id, task.version, c, plan)
            else:
                self.repo.event(f"requirement:{task.task_id}:{task.version}", task.task_id, {"text": task.message})
            return "需求已建立"
        task = self.repo.task(p["task_id"])
        if task.version != p["expected_version"]:
            raise ValueError("任務版本已改變，請刷新後重試")
        if kind == "approve":
            self.repo.approve(task.task_id, task.version, p["max_cost"])
        elif kind == "cancel":
            if task.status not in {"needs_details", "pending", "scheduled"}:
                raise ValueError("已進入啟動／運轉，請核對設備並確認中止")
            self.repo.update_task(task.task_id, task.version, status="cancelled", message="使用者取消")
        elif kind == "details":
            if task.status not in {"needs_details", "pending", "scheduled"}:
                raise ValueError("目前狀態不可補充／改排")
            # A changed requirement suspends the old start immediately, while the
            # original envelope remains available for an in-scope reschedule.
            task = self.repo.update_task(task.task_id, task.version, message=p["text"], status="needs_details", plan=None)
            self.repo.event(f"requirement:{task.task_id}:{task.version}", task.task_id, {"text": p["text"]})
        elif kind == "schedule_form":
            c = decode(TaskConstraints, p["constraints"])
            plan = find_schedule(c, task.program, task.tariff, clock.now(real), clock.clock_id)
            self.repo.propose(task.task_id, task.version, c, plan, task.approval is not None, "使用者表單改排")
        elif kind == "abort":
            state = self.state()
            if p.get("confirmed") is not True or task.status not in {"running", "unknown", "starting"}:
                raise ValueError("中止需要使用者再次確認，且任務必須已啟動")
            if not fresh(state, real) or state.status != "running" or state.task_id != task.task_id or state.run_id != task.run_id:
                raise ValueError("尚未取得同一行程的運轉狀態，請先查核設備")
            task = self.repo.update_task(task.task_id, task.version, status="stopping", message="等待設備中止確認")
            self.send("stop", task, real)
        elif kind == "reset":
            state = self.state()
            if p.get("confirmed") is not True or task.status not in {"failed", "unknown"} or not fresh(state, real):
                raise ValueError("請先人工檢查設備，取得新鮮狀態並確認解除")
            if state.status == "fault":
                command = self.send("reset", task, real)
                self.repo.set_meta("reset_pending", {"task_id": task.task_id, "command_id": command.command_id})
            elif state.status in {"idle", "completed", "aborted"}:
                commands = self.repo.records("commands", 100)
                pending = [decode(DeviceCommand, json.loads(r["data"])) for r in commands]
                if any(c.task_id == task.task_id and c.action in {"start", "stop"} and state.observed_at <= c.expires_at for c in pending):
                    raise ValueError("請等待舊命令過期後再次查核設備，避免仍在傳輸中的命令")
                self.repo.update_task(task.task_id, task.version, status="resolved", message="人工核對設備已停止並解除")
            else:
                raise ValueError("設備仍在運轉，請先確認中止")
        elif kind == "query":
            self.query(real)
        else:
            raise ValueError("未知 UI 請求")
        return "操作已處理；設備動作仍需狀態確認"

    def anomaly(self, task, reason: str):
        key = "anomaly:" + task.task_id
        value = {"version": task.version, "reason": reason}
        if self.repo.meta(key) != value:
            self.repo.event("execution:" + task.task_id, task.task_id, {"reason": reason})
            self.repo.set_meta(key, value)

    def reconcile(self, real: datetime):
        state, task = self.state(), self.repo.active_task()
        clock = self.repo.clock()
        valid = fresh(state, real) and state.observed_at >= self.started_at and state.clock_id == clock.clock_id and self.transport.connected
        if valid:
            self.recovered = True
        if task is None:
            return
        if not valid:
            if task.status in {"starting", "running", "stopping"}:
                task = self.repo.update_task(task.task_id, task.version, status="unknown", message="通訊／時鐘狀態未知；保留行程名額")
                self.anomaly(task, task.message)
            self.query(real)
            return
        reset = self.repo.meta("reset_pending")
        if reset and reset["task_id"] == task.task_id and state.status == "idle" and state.last_command_id == reset["command_id"]:
            self.repo.update_task(task.task_id, task.version, status="resolved", message="設備已確認人工故障解除")
            self.repo.set_meta("reset_pending", None)
            return
        matches = state.task_id == task.task_id and state.run_id == task.run_id and task.run_id is not None
        if task.status in {"starting", "running", "stopping", "unknown"}:
            if state.status == "fault":
                task = self.repo.update_task(task.task_id, task.version, status="failed", message=state.fault or "設備故障")
                self.anomaly(task, task.message)
            elif matches:
                next_status = {"running": "running", "completed": "completed", "aborted": "aborted"}.get(state.status)
                if next_status and not (task.status == "stopping" and next_status == "running") and next_status != task.status:
                    self.repo.update_task(task.task_id, task.version, status=next_status, message="已依設備狀態確認")
                    if next_status in {"completed", "aborted"}:
                        self.repo.event("finished:" + task.task_id, task.task_id, {"status": next_status})
            elif task.status == "starting" and any(c.task_id == task.task_id and c.action == "start" and self.monotonic() - sent < 5 for sent, c in self.waits.values()):
                return
            elif task.status != "unknown":
                task = self.repo.update_task(task.task_id, task.version, status="unknown", message="設備行程識別碼不符；需要人工查核")
                self.anomaly(task, task.message)

    def execute_due(self, real: datetime):
        with self.repo.transaction():
            self._execute_due(real)

    def _execute_due(self, real: datetime):
        task = self.repo.active_task()
        if not task or task.status != "scheduled" or not task.plan or not task.constraints or not task.approval:
            return
        clock = self.repo.clock()
        now = clock.now(real)
        if now < task.plan.start:
            return
        state = self.state()
        if not self.recovered or not fresh(state, real) or not self.transport.connected or state.clock_id != clock.clock_id or state.status not in {"idle", "completed", "aborted"}:
            self.anomaly(task, "到時設備不可用；需改排或查核設備")
            return
        if (now - task.plan.start).total_seconds() > 3 * clock.speed:
            self.anomaly(task, "錯過開始時間；請在核准範圍內改排")
            return
        try:
            # Tick/network latency may shift actual start. Recheck the whole shifted run.
            actual = make_plan(now, task.plan.program, task.plan.tariff, clock.clock_id)
            validate_plan(actual, task.constraints, now, clock.clock_id, task.approval)
        except ValueError as error:
            self.anomaly(task, str(error))
            return
        with self.repo.transaction():
            task = self.repo.update_task(task.task_id, task.version, status="starting", run_id=uuid4().hex,
                                         message="等待設備啟動狀態")
            self.send("start", task, real)

    def tick(self, real: datetime | None = None):
        real = real or utcnow()
        self.ingest(real)
        self.process_requests(real)
        self.reconcile(real)
        self.execute_due(real)
        for command_id, (started, command) in list(self.waits.items()):
            if self.monotonic() - started >= 5:
                del self.waits[command_id]
                self.repo.audit("confirmation_timeout", command)
                if command.action in {"start", "stop", "reset"}:
                    task = self.repo.active_task()
                    if task and task.task_id == command.task_id:
                        if task.status in {"starting", "stopping"}:
                            task = self.repo.update_task(task.task_id, task.version, status="unknown", message="命令逾時；查詢狀態，沒有重送啟動")
                        self.anomaly(task, "命令確認逾時，已查詢狀態")
                    self.query(real)
        self.repo.set_meta("controller_heartbeat", real)


def main():
    from smart_home_energy.agent import agent_worker
    from smart_home_energy.service_lock import service_lock
    logging.basicConfig(level=logging.INFO)
    settings, hems = Settings.from_env(), HemsSettings.from_env()
    with service_lock(hems.database + ".control.lock"):
        repo = Repository(hems.database)
        repo.recover_events()
        transport = WasherTransport(settings, hems.mqtt_root, "control")
        controller = Controller(repo, transport, hems)
        stopped = threading.Event()
        worker = threading.Thread(target=agent_worker, args=(repo, hems, stopped), daemon=True, name="main-agent")
        transport.start()
        worker.start()
        try:
            while True:
                controller.tick()
                stopped.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
            worker.join(timeout=2)
            transport.stop()


if __name__ == "__main__":
    main()

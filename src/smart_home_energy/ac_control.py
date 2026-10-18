"""AC uses the same durable request, confirmation and recovery lifecycle."""
from dataclasses import replace
from datetime import datetime, timedelta
import json
from uuid import uuid4

from smart_home_energy.ac import ACCommand, ACState
from smart_home_energy.ac_domain import ACConstraints, make_ac_plan, validate_ac
from smart_home_energy.control import Controller, fresh
from smart_home_energy.domain import decode, utcnow
from smart_home_energy.tariffs import selected_tariff


class ACController(Controller):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, device_id="ac-1", **kwargs)

    def state(self):
        raw = self.repo.device("ac-1")
        return decode(ACState, raw) if raw else None

    def send(self, action, task=None, real=None):
        real = real or utcnow()
        state = self.state()
        if not state or not self.transport.connected:
            raise ValueError("冷氣或傳輸尚未連線")
        command = ACCommand(uuid4().hex, "ac-1", action, self.repo.clock().clock_id, real + timedelta(seconds=5), state.boot_id,
            task.task_id if task else None, task.version if task else 0, task.run_id if task else None,
            task.ac_plan if task and action == "start" else None, task.ac_approval if task and action == "start" else None)
        self.repo.save_command(command)
        self.waits[command.command_id] = (self.monotonic(), command)
        self.transport.publish("command", command)
        return command

    def handle_request(self, kind, p, real):
        if kind == "create_ac_form":
            c = decode(ACConstraints, p["constraints"])
            task = self.repo.create_ac_task(c, selected_tariff(self.repo, self.hems.tariff()))
        else:
            task = self.repo.task(p["task_id"])
            if task.device_id != "ac-1" or task.version != p["expected_version"]:
                raise ValueError("冷氣任務或版本不符")
        if kind in {"create_ac_form", "schedule_ac_form"}:
            c = decode(ACConstraints, p["constraints"])
            if task.status in {"needs_details", "pending"}:
                task = self.repo.update_task(task.task_id, task.version, tariff=selected_tariff(self.repo, self.hems.tariff(), task.task_id))
            state = self.state()
            if not fresh(state, real):
                raise ValueError("需要新鮮的冷氣室溫才能估算計畫；需求已保存，可稍後重算")
            plan = make_ac_plan(c, state.temperature_c, task.tariff, self.repo.clock().clock_id)
            self.repo.propose_ac(task.task_id, task.version, plan)
        elif kind == "approve":
            self.repo.approve_ac(task.task_id, task.version)
        elif kind == "reset":
            state = self.state()
            if p.get("confirmed") is not True or task.status not in {"unknown", "failed"} or not fresh(state, real):
                raise ValueError("需要人工確認及新鮮設備狀態")
            if state.status == "fault":
                cmd = self.send("reset", task, real)
                self.repo.set_meta("reset_pending:ac-1", {"task_id": task.task_id, "command_id": cmd.command_id})
            elif state.status in {"idle", "completed", "aborted"}:
                pending = [json.loads(row["data"]) for row in self.repo.records("commands", 100)]
                if any(row.get("task_id") == task.task_id and row["action"] in {"start", "stop"} and state.observed_at <= datetime.fromisoformat(row["expires_at"]) for row in pending):
                    raise ValueError("請等待舊命令過期後查核")
                self.repo.update_task(task.task_id, task.version, status="resolved")
            else:
                raise ValueError("請先確認中止運轉中的冷氣")
        else:
            return super().handle_request(kind, p, real)
        return "冷氣請求已處理；設備操作仍需狀態確認"

    def _execute_due(self, real):
        task = self.repo.active_task("ac-1")
        if not task or task.status != "scheduled" or not task.ac_plan or not task.ac_approval:
            return
        clock, state = self.repo.clock(), self.state()
        now = clock.now(real)
        c = task.ac_plan.constraints
        if now < c.start:
            return
        if not self.recovered or not fresh(state, real) or not self.transport.connected or state.clock_id != clock.clock_id or state.status not in {"idle", "completed", "aborted"}:
            self.anomaly(task, "到時冷氣不可用；先核對狀態")
            return
        if now >= c.end:
            self.anomaly(task, "已錯過冷氣核准時段，請提出新計畫")
            return
        try:
            actual = make_ac_plan(replace(c, start=now), state.temperature_c, task.tariff, clock.clock_id)
            validate_ac(actual, now, clock.clock_id, task.ac_approval)
        except ValueError as error:
            self.anomaly(task, str(error))
            return
        task = self.repo.update_task(task.task_id, task.version, status="starting", run_id=uuid4().hex, message="等待冷氣啟動確認")
        self.send("start", task, real)

    def reconcile(self, real):
        super().reconcile(real)
        task, state = self.repo.active_task("ac-1"), self.state()
        if not task or task.status != "running" or not fresh(state, real) or not task.ac_plan or state.run_id != task.run_id:
            return
        c = task.ac_plan.constraints
        elapsed = (self.repo.clock().now(real) - c.start).total_seconds()
        warmup = task.ac_plan.comfortable_after_seconds
        drift_key = "ac_drift_since:" + task.task_id
        outside = not c.minimum_c - .2 <= state.temperature_c <= c.maximum_c + .2
        since = self.repo.meta(drift_key)
        if elapsed > (warmup or 0) and outside:
            if since is None:
                self.repo.set_meta(drift_key, elapsed)
            elif elapsed - since >= 120:
                self.anomaly(task, "冷氣持續偏離舒適範圍，請檢查或提出限制內調整")
        elif since is not None:
            self.repo.set_meta(drift_key, None)
        if state.cost > task.ac_plan.cost * 1.1 + .01:
            self.anomaly(task, "冷氣費用高於預估；舒適優先，沒有因費用關機")

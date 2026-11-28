"""Resident controller for curtain durable requests, state sync, and logical-clock schedules."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from smart_home_energy import curtain
from smart_home_energy.domain import aware, utcnow

CURTAIN_REQUEST_KINDS = {
    "curtain_position",
    "curtain_stop",
    "curtain_scene",
    "curtain_schedule_create",
    "curtain_schedule_toggle",
    "curtain_schedule_delete",
}

_SCENES = {"open": 100.0, "half": 50.0, "close": 0.0}
_SCENE_LABELS = {"open": "全開 (100%)", "half": "半開 (50%)", "close": "全關 (0%)"}


class CurtainController:
    """Processes durable curtain requests and triggers daily schedules on `repo.clock()`."""

    def __init__(self, repo):
        self.repo = repo

    def handle_request(self, request_id: str, kind: str, payload: dict, real: datetime) -> str:
        auth_ref = f"request:{request_id}"
        source = str(payload.get("source") or "ui")

        if kind == "curtain_position":
            pos = float(payload["position"])
            duration = float(payload.get("duration_seconds", 3.0))
            updated = curtain.move_to(
                pos,
                duration,
                repo=self.repo,
                source=source,
                authorization_ref=auth_ref,
                real=real,
            )
            return f"已送出指令：窗簾移動至 {round(float(updated['target']))}%"

        if kind == "curtain_stop":
            updated = curtain.stop_curtain(
                repo=self.repo,
                source=source,
                authorization_ref=auth_ref,
                real=real,
            )
            return f"窗簾已停止，目前位置 {round(float(updated['position']))}%"

        if kind == "curtain_scene":
            scene = str(payload["scene"])
            if scene not in _SCENES:
                raise ValueError("場景僅支援 open、half、close")
            curtain.move_to(
                _SCENES[scene],
                float(payload.get("duration_seconds", 2.0)),
                repo=self.repo,
                source=source,
                authorization_ref=auth_ref,
                action_name=f"scene:{scene}",
                real=real,
            )
            return f"場景已執行：{_SCENE_LABELS[scene]}"

        if kind == "curtain_schedule_create":
            created = curtain.create_schedule_entry(
                str(payload["time"]),
                float(payload["position"]),
                enabled=bool(payload.get("enabled", True)),
                repo=self.repo,
                source=source,
                authorization_ref=auth_ref,
                real=real,
            )
            return f"已新增排程：{created['time']} → {round(float(created['position']))}%"

        if kind == "curtain_schedule_toggle":
            try:
                updated_list = curtain.set_schedule_enabled_entry(
                    schedule_id=payload.get("schedule_id"),
                    time_str=payload.get("time"),
                    enabled=payload.get("enabled"),
                    repo=self.repo,
                    source=source,
                    authorization_ref=auth_ref,
                    real=real,
                )
            except KeyError as err:
                raise ValueError(str(err).strip("'\"")) from err
            item = updated_list[0]
            label = "啟用" if item["enabled"] else "停用"
            return f"已{label}排程：{item['time']}"

        if kind == "curtain_schedule_delete":
            try:
                removed = curtain.delete_schedule_entry(
                    schedule_id=payload.get("schedule_id"),
                    time_str=payload.get("time"),
                    delete_all=bool(payload.get("delete_all", False)),
                    repo=self.repo,
                    source=source,
                    authorization_ref=auth_ref,
                    real=real,
                )
            except KeyError as err:
                raise ValueError(str(err).strip("'\"")) from err
            times = "、".join(item["time"] for item in removed)
            return f"排程已刪除：{times}"

        raise ValueError(f"未知的窗簾請求類型：{kind}")

    def process_requests(self, real: datetime) -> None:
        for request in self.repo.pending_requests():
            if request["kind"] not in CURTAIN_REQUEST_KINDS:
                continue
            try:
                with self.repo.transaction():
                    payload = json.loads(request["payload"])
                    message = self.handle_request(request["id"], request["kind"], payload, real)
                    self.repo.finish_request(request["id"], "done", message)
            except (ValueError, TypeError, KeyError, RuntimeError, sqlite3.IntegrityError) as error:
                self.repo.finish_request(request["id"], "error", str(error))

    def tick(self, real: datetime | None = None) -> None:
        now_real = aware(real or utcnow())
        curtain.tick(repo=self.repo, real=now_real)
        self.process_requests(now_real)
        curtain.evaluate_due_schedules(self.repo, real=now_real)

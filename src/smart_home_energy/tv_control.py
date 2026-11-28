"""Resident controller for TV durable requests and SQLite state/audit synchronization."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from smart_home_energy import tv
from smart_home_energy.domain import aware, utcnow

TV_REQUEST_KINDS = {"tv_control"}


class TVController:
    """Processes durable TV control requests and keeps `tv-1` synced to SQLite."""

    def __init__(self, repo):
        self.repo = repo

    def handle_request(self, request_id: str, kind: str, payload: dict, real: datetime) -> str:
        if kind != "tv_control":
            raise ValueError(f"未知的電視請求類型：{kind}")

        auth_ref = f"request:{request_id}"
        source = str(payload.get("source") or "ui")
        power = payload.get("power")
        volume = payload.get("volume")
        muted = payload.get("muted")
        input_source = payload.get("input")
        playback = payload.get("playback")

        if all(val is None for val in (power, volume, muted, input_source, playback)):
            raise ValueError("tv_control 至少需指定一項電視控制參數")

        updated = tv.update_tv(
            power=power,
            volume=volume,
            muted=muted,
            input_source=input_source,
            playback=playback,
            repo=self.repo,
            source=source,
            authorization_ref=auth_ref,
            real=real,
        )

        changes: list[str] = []
        if power is True:
            changes.append("開啟電視")
        elif power is False:
            changes.append("關閉電視（進入待機）")
        if input_source is not None:
            changes.append(f"切換至 {updated['input']}")
        if volume is not None:
            changes.append(f"音量調整至 {updated['volume']}%")
        if muted is True:
            changes.append("開啟靜音")
        elif muted is False and volume is None:
            changes.append("解除靜音")
        if playback is not None and updated["power"]:
            labels = {"playing": "開始播放", "paused": "已暫停播放", "stopped": "已停止播放"}
            changes.append(labels[str(updated["playback"])])

        if changes:
            return "客廳電視已執行：" + "、".join(changes)
        return f"客廳電視狀態已更新（訊號源 {updated['input']}，音量 {updated['volume']}%）"

    def process_requests(self, real: datetime) -> None:
        for request in self.repo.pending_requests():
            if request["kind"] not in TV_REQUEST_KINDS:
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
        tv.get_status(repo=self.repo)
        self.process_requests(now_real)

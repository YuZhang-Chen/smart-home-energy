"""Virtual curtain service with durable operation logs, logical-clock schedules, and MCP Curtain Agent.

Run standalone with:
    python -m uvicorn smart_home_energy.curtain:app --app-dir src --host 127.0.0.1 --port 8001

When mounted inside the main HEMS app/controller, schedules follow `repo.clock()`
(supporting 60x demo mode) and every state/schedule mutation records durable
`devices`, `commands`, and `audit` logs in SQLite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import inspect
import json
from pathlib import Path
import re
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from smart_home_energy.domain import TAIPEI, aware, encode, utcnow

lock = RLock()
stop_event = Event()

DATA_FILE = Path(__file__).resolve().parents[2] / "artifacts" / "runtime" / "curtain_schedules.json"
STATE_FILE = Path(__file__).resolve().parents[2] / "artifacts" / "runtime" / "curtain_state.json"

SCHEDULES_META_KEY = "curtain_schedules"
SCHEDULER_STATE_KEY = "curtain_scheduler_state"

state: dict[str, Any] = {
    "device_id": "curtain-1",
    "position": 0.0,
    "target": 0.0,
    "status": "stopped",
    "duration_seconds": 2.0,
    "updated_at": datetime.now().astimezone().isoformat(),
}
schedules: dict[str, dict[str, Any]] = {}
last_tick = monotonic()


@dataclass(frozen=True)
class CurtainDeviceState:
    device_id: str
    position: float
    target: float
    status: str
    duration_seconds: float
    updated_at: str
    observed_at: datetime
    logical_at: datetime


@dataclass(frozen=True)
class CurtainCommand:
    command_id: str
    device_id: str
    action: str
    target_position: float | None
    duration_seconds: float | None
    source: str
    authorization_ref: str
    logical_at: datetime


@dataclass(frozen=True)
class CurtainCommandResult:
    command_id: str
    device_id: str
    accepted: bool
    message: str
    position: float
    target: float
    status: str
    observed_at: datetime


def _logical_now(repo: Any, real: datetime | None = None) -> datetime:
    now_real = aware(real or utcnow())
    if repo is not None:
        return repo.clock().now(now_real).astimezone(TAIPEI)
    return now_real.astimezone(TAIPEI)


def _sync_device_to_repo(repo: Any, real: datetime | None = None) -> None:
    if repo is None:
        return
    now_real = aware(real or utcnow())
    logical = _logical_now(repo, now_real)
    snapshot = CurtainDeviceState(
        device_id="curtain-1",
        position=round(float(state["position"]), 1),
        target=round(float(state["target"]), 1),
        status=str(state["status"]),
        duration_seconds=float(state.get("duration_seconds", 2.0)),
        updated_at=str(state["updated_at"]),
        observed_at=now_real,
        logical_at=logical,
    )
    repo.save_device(snapshot)


def _record_command_and_audit(
    repo: Any,
    *,
    action: str,
    audit_kind: str,
    source: str,
    authorization_ref: str,
    before: dict[str, Any],
    after: dict[str, Any],
    message: str,
    target_position: float | None = None,
    duration_seconds: float | None = None,
    extra: dict[str, Any] | None = None,
    real: datetime | None = None,
) -> None:
    if repo is None:
        return
    now_real = aware(real or utcnow())
    logical = _logical_now(repo, now_real)
    cmd = CurtainCommand(
        command_id=uuid4().hex,
        device_id="curtain-1",
        action=action,
        target_position=target_position,
        duration_seconds=duration_seconds,
        source=source,
        authorization_ref=authorization_ref,
        logical_at=logical,
    )
    repo.save_command(cmd)
    res = CurtainCommandResult(
        command_id=cmd.command_id,
        device_id="curtain-1",
        accepted=True,
        message=message,
        position=round(float(after["position"]), 1),
        target=round(float(after["target"]), 1),
        status=str(after["status"]),
        observed_at=now_real,
    )
    repo.command_result(res)
    audit_payload: dict[str, Any] = {
        "device_id": "curtain-1",
        "command_id": cmd.command_id,
        "action": action,
        "source": source,
        "authorization_ref": authorization_ref,
        "logical_at": logical.isoformat(),
        "before": {
            "position": round(float(before["position"]), 1),
            "target": round(float(before["target"]), 1),
            "status": str(before["status"]),
        },
        "after": {
            "position": round(float(after["position"]), 1),
            "target": round(float(after["target"]), 1),
            "status": str(after["status"]),
        },
        "message": message,
    }
    if extra:
        audit_payload.update(extra)
    repo.audit(audit_kind, audit_payload)


def _load_state_unlocked() -> None:
    global last_tick
    if not STATE_FILE.exists():
        return
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        prev_updated = state.get("updated_at")
        new_updated = raw.get("updated_at")
        if new_updated != prev_updated:
            state["position"] = float(raw.get("position", state["position"]))
            state["target"] = float(raw.get("target", state["target"]))
            state["status"] = str(raw.get("status", state["status"]))
            state["duration_seconds"] = float(raw.get("duration_seconds", 2.0))
            state["updated_at"] = str(new_updated or datetime.now().astimezone().isoformat())
            last_tick = monotonic()
    except (OSError, ValueError, TypeError):
        pass


def _save_state_unlocked() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temp.replace(STATE_FILE)
    except OSError:
        pass


def _normalize_schedule_list(raw: Any) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        raise ValueError("排程資料格式錯誤")
    for item in raw:
        if not isinstance(item, dict):
            continue
        schedule_id = item.get("id")
        schedule_time = item.get("time")
        position = item.get("position")
        if not isinstance(schedule_id, str) or not isinstance(schedule_time, str):
            continue
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
            continue
        if isinstance(position, bool) or not isinstance(position, (int, float)) or not 0 <= position <= 100:
            continue
        normalized[schedule_id] = {
            "id": schedule_id,
            "time": schedule_time,
            "position": float(position),
            "enabled": bool(item.get("enabled", True)),
            "last_run_date": item.get("last_run_date"),
        }
    return normalized


def load_schedules(repo: Any = None) -> dict[str, dict[str, Any]]:
    with lock:
        if repo is not None:
            stored = repo.meta(SCHEDULES_META_KEY)
            if stored is not None:
                parsed = _normalize_schedule_list(stored)
                schedules.clear()
                schedules.update(parsed)
                return {k: dict(v) for k, v in schedules.items()}
            # Fresh repository: keep empty in repo unless explicitly added.
            schedules.clear()
            return {}
        schedules.clear()
        if not DATA_FILE.exists():
            return {}
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            parsed = _normalize_schedule_list(raw)
            schedules.update(parsed)
            return {k: dict(v) for k, v in schedules.items()}
        except (OSError, ValueError, TypeError) as exc:
            # Do not silently overwrite a damaged schedules file.
            raise RuntimeError(f"無法讀取窗簾排程資料：{DATA_FILE}: {exc}") from exc


def save_schedules(repo: Any = None) -> None:
    """Caller must hold lock; persist to SQLite meta (authoritative when repo given) and mirror JSON."""
    values = [dict(item) for item in schedules.values()]
    if repo is not None:
        repo.set_meta(SCHEDULES_META_KEY, values)
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(DATA_FILE)
    except OSError:
        if repo is None:
            raise


def list_schedules_data(repo: Any = None) -> list[dict[str, Any]]:
    with lock:
        if repo is not None:
            load_schedules(repo)
        return [dict(item) for item in schedules.values()]


def create_schedule_entry(
    time_str: str,
    position: float,
    enabled: bool = True,
    *,
    repo: Any = None,
    source: str = "rest_api",
    authorization_ref: str = "direct",
    real: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(time_str, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_str):
        raise ValueError("時間格式錯誤，請使用 HH:MM（24 小時制）")
    if isinstance(position, bool) or not isinstance(position, (int, float)) or not 0 <= float(position) <= 100:
        raise ValueError("排程窗簾位置須為 0～100")
    with lock:
        if repo is not None:
            load_schedules(repo)
        schedule_id = uuid4().hex
        item = {
            "id": schedule_id,
            "time": time_str,
            "position": float(position),
            "enabled": bool(enabled),
            "last_run_date": None,
        }
        schedules[schedule_id] = item
        try:
            save_schedules(repo)
        except OSError as exc:
            del schedules[schedule_id]
            raise RuntimeError(f"排程儲存失敗：{exc}") from exc
        if repo is not None:
            repo.audit(
                "curtain_schedule_created",
                {
                    "device_id": "curtain-1",
                    "schedule": dict(item),
                    "source": source,
                    "authorization_ref": authorization_ref,
                    "logical_at": _logical_now(repo, real).isoformat(),
                },
            )
        return dict(item)


def set_schedule_enabled_entry(
    *,
    schedule_id: str | None = None,
    time_str: str | None = None,
    enabled: bool | None = None,
    repo: Any = None,
    source: str = "rest_api",
    authorization_ref: str = "direct",
    real: datetime | None = None,
) -> list[dict[str, Any]]:
    if schedule_id is None and time_str is None:
        raise ValueError("請指定要啟用或停用的排程 ID 或時間（HH:MM）")
    if time_str is not None and (not isinstance(time_str, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_str)):
        raise ValueError("排程時間格式須為 HH:MM（24 小時制）")
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("enabled 須為布林值")
    with lock:
        if repo is not None:
            load_schedules(repo)
        targets: list[dict[str, Any]] = []
        if schedule_id is not None:
            item = schedules.get(schedule_id)
            if item is None:
                raise KeyError("找不到排程")
            targets.append(item)
        else:
            targets = [item for item in schedules.values() if item["time"] == time_str]
            if not targets:
                available = "、".join(sorted({item["time"] for item in schedules.values()})) or "目前無排程"
                raise KeyError(f"找不到 {time_str} 的窗簾排程（現有排程：{available}）")
        previous_states = [(item["id"], item["enabled"]) for item in targets]
        for item in targets:
            item["enabled"] = (not item["enabled"]) if enabled is None else bool(enabled)
        try:
            save_schedules(repo)
        except OSError as exc:
            for item_id, prev_enabled in previous_states:
                if item_id in schedules:
                    schedules[item_id]["enabled"] = prev_enabled
            raise RuntimeError(f"排程儲存失敗：{exc}") from exc
        updated = [dict(item) for item in targets]
        if repo is not None:
            repo.audit(
                "curtain_schedule_toggled",
                {
                    "device_id": "curtain-1",
                    "schedules": updated,
                    "source": source,
                    "authorization_ref": authorization_ref,
                    "logical_at": _logical_now(repo, real).isoformat(),
                },
            )
        return updated


def delete_schedule_entry(
    *,
    schedule_id: str | None = None,
    time_str: str | None = None,
    delete_all: bool = False,
    repo: Any = None,
    source: str = "rest_api",
    authorization_ref: str = "direct",
    real: datetime | None = None,
) -> list[dict[str, Any]]:
    if not delete_all and schedule_id is None and time_str is None:
        raise ValueError("請指定要刪除的排程 ID、時間（HH:MM），或指定 delete_all=true")
    if time_str is not None and (not isinstance(time_str, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_str)):
        raise ValueError("排程時間格式須為 HH:MM（24 小時制）")
    with lock:
        if repo is not None:
            load_schedules(repo)
        removed: list[dict[str, Any]] = []
        if delete_all:
            if not schedules:
                raise KeyError("目前沒有任何窗簾排程可刪除")
            removed = [dict(item) for item in schedules.values()]
            backup = dict(schedules)
            schedules.clear()
        elif schedule_id is not None:
            if schedule_id not in schedules:
                raise KeyError("找不到排程")
            backup = dict(schedules)
            removed = [dict(schedules.pop(schedule_id))]
        else:
            matching_ids = [sid for sid, item in schedules.items() if item["time"] == time_str]
            if not matching_ids:
                available = "、".join(sorted({item["time"] for item in schedules.values()})) or "目前無排程"
                raise KeyError(f"找不到 {time_str} 的窗簾排程（現有排程：{available}）")
            backup = dict(schedules)
            for sid in matching_ids:
                removed.append(dict(schedules.pop(sid)))
        try:
            save_schedules(repo)
        except OSError as exc:
            schedules.clear()
            schedules.update(backup)
            raise RuntimeError(f"排程儲存失敗：{exc}") from exc
        if repo is not None:
            repo.audit(
                "curtain_schedule_deleted",
                {
                    "device_id": "curtain-1",
                    "deleted": removed,
                    "source": source,
                    "authorization_ref": authorization_ref,
                    "logical_at": _logical_now(repo, real).isoformat(),
                },
            )
        return removed


def tick(repo: Any = None, real: datetime | None = None) -> None:
    """Advance the virtual curtain according to elapsed time across processes."""
    global last_tick
    with lock:
        _load_state_unlocked()
        now = monotonic()
        elapsed = max(0.0, min(now - last_tick, 1.0))
        try:
            wall_elapsed = (datetime.now().astimezone() - datetime.fromisoformat(str(state["updated_at"]))).total_seconds()
            if wall_elapsed > elapsed:
                elapsed = min(wall_elapsed, 10.0)
        except (ValueError, TypeError):
            pass
        last_tick = now
        if state["status"] != "moving":
            if repo is not None and repo.device("curtain-1") is None:
                _sync_device_to_repo(repo, real)
            return
        distance = state["target"] - state["position"]
        duration = state.get("duration_seconds", 2.0)
        speed = 100.0 / max(duration, 0.1)
        step = speed * elapsed
        if abs(distance) <= step:
            state["position"] = state["target"]
            state["status"] = "stopped"
        else:
            state["position"] += (1 if distance > 0 else -1) * step
        state["updated_at"] = datetime.now().astimezone().isoformat()
        _save_state_unlocked()
        _sync_device_to_repo(repo, real)


def move_to(
    position: float,
    duration: float = 2.0,
    *,
    repo: Any = None,
    source: str = "rest_api",
    authorization_ref: str = "direct",
    action_name: str = "set_position",
    extra_audit: dict[str, Any] | None = None,
    real: datetime | None = None,
) -> dict[str, Any]:
    tick(repo=repo, real=real)
    with lock:
        before = dict(state)
        state["target"] = float(position)
        state["duration_seconds"] = float(duration)
        if duration <= 0 or abs(state["target"] - state["position"]) < 0.01:
            state["position"] = state["target"]
            state["status"] = "stopped"
        else:
            state["status"] = "moving"
        state["updated_at"] = datetime.now().astimezone().isoformat()
        _save_state_unlocked()
        _sync_device_to_repo(repo, real)
        after = dict(state)
        target_round = round(float(after["target"]))
        label = "全開 (100%)" if target_round >= 99 else "全關 (0%)" if target_round <= 1 else f"開啟 {target_round}%"
        _record_command_and_audit(
            repo,
            action=action_name,
            audit_kind="curtain_position_set",
            source=source,
            authorization_ref=authorization_ref,
            before=before,
            after=after,
            message=f"客廳窗簾移動至 {label}",
            target_position=float(position),
            duration_seconds=float(duration),
            extra=extra_audit,
            real=real,
        )
        return after


def get_status(repo: Any = None, real: datetime | None = None) -> dict[str, Any]:
    tick(repo=repo, real=real)
    with lock:
        return {**state, "position": round(state["position"], 1), "target": round(state["target"], 1)}


def stop_curtain(
    *,
    repo: Any = None,
    source: str = "rest_api",
    authorization_ref: str = "direct",
    real: datetime | None = None,
) -> dict[str, Any]:
    tick(repo=repo, real=real)
    with lock:
        before = dict(state)
        state["target"] = state["position"]
        state["status"] = "stopped"
        state["updated_at"] = datetime.now().astimezone().isoformat()
        _save_state_unlocked()
        _sync_device_to_repo(repo, real)
        after = dict(state)
        pos_round = round(float(after["position"]))
        _record_command_and_audit(
            repo,
            action="stop",
            audit_kind="curtain_stopped",
            source=source,
            authorization_ref=authorization_ref,
            before=before,
            after=after,
            message=f"客廳窗簾已停止移動，目前位置 {pos_round}%",
            target_position=float(after["position"]),
            duration_seconds=0.0,
            real=real,
        )
        return after


def _extract_time_hhmm(text: str) -> str | None:
    hhmm = re.search(r"((?:[01]\d|2[0-3]):[0-5]\d)", text)
    if hhmm:
        return hhmm.group(1)
    zh = re.search(r"(早上|上午|中午|下午|晚上|晚間)?\s*(\d{1,2})\s*(?:點|時)(?:(半)|(\d{1,2})\s*分?)?", text)
    if zh:
        period, hour_s, half, minute_s = zh.groups()
        hour = int(hour_s)
        minute = 30 if half else (int(minute_s) if minute_s else 0)
        if period in {"下午", "晚上", "晚間"} and 1 <= hour <= 11:
            hour += 12
        elif period in {"早上", "上午"} and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return None


def execute_command_with_summary(
    command: str,
    *,
    repo: Any = None,
    source: str = "rest_api",
    authorization_ref: str = "direct",
) -> tuple[str, dict[str, Any]]:
    text = command.replace(" ", "")
    time_str = _extract_time_hhmm(text)
    is_schedule_intent = any(word in text for word in ("排程", "定時", "每天", "預約"))

    if is_schedule_intent:
        if any(word in text for word in ("刪除", "移除", "取消", "清空")):
            delete_all = any(word in text for word in ("全部", "所有", "清空"))
            try:
                removed = delete_schedule_entry(
                    time_str=None if delete_all else time_str,
                    delete_all=delete_all,
                    repo=repo,
                    source=source,
                    authorization_ref=authorization_ref,
                )
            except KeyError as err:
                raise ValueError(str(err).strip("'\"")) from err
            times = "、".join(item["time"] for item in removed)
            return f"已刪除客廳窗簾排程（{times}）。", get_status(repo=repo)

        if any(word in text for word in ("停用", "關閉排程", "暫停", "關掉排程", "不用")):
            try:
                updated = set_schedule_enabled_entry(
                    time_str=time_str,
                    enabled=False,
                    repo=repo,
                    source=source,
                    authorization_ref=authorization_ref,
                )
            except KeyError as err:
                raise ValueError(str(err).strip("'\"")) from err
            times = "、".join(item["time"] for item in updated)
            return f"已停用客廳窗簾排程（{times}）。", get_status(repo=repo)

        if any(word in text for word in ("啟用", "恢復", "重新開啟", "開啟排程", "打開排程")):
            try:
                updated = set_schedule_enabled_entry(
                    time_str=time_str,
                    enabled=True,
                    repo=repo,
                    source=source,
                    authorization_ref=authorization_ref,
                )
            except KeyError as err:
                raise ValueError(str(err).strip("'\"")) from err
            times = "、".join(item["time"] for item in updated)
            return f"已啟用客廳窗簾排程（{times}）。", get_status(repo=repo)

        if time_str:
            target_pos = 100.0
            if any(word in text for word in ("半開", "一半", "50%")):
                target_pos = 50.0
            elif any(word in text for word in ("全關", "關閉", "關上", "拉上", "0%")):
                target_pos = 0.0
            else:
                pct = re.search(r"(\d{1,3})%", text)
                if pct:
                    target_pos = float(min(100, int(pct.group(1))))
            create_schedule_entry(
                time_str,
                target_pos,
                enabled=True,
                repo=repo,
                source=source,
                authorization_ref=authorization_ref,
            )
            return (
                f"已為客廳窗簾新增每日 {time_str} 排程（目標位置 {round(target_pos)}%）。",
                get_status(repo=repo),
            )

    if any(word in text for word in ("停止", "暫停", "停下")):
        st = stop_curtain(repo=repo, source=source, authorization_ref=authorization_ref)
        return f"客廳窗簾已停止移動，目前位置 {round(float(st['position']))}%。", st
    if any(word in text for word in ("半開", "一半", "半關")):
        st = move_to(50, repo=repo, source=source, authorization_ref=authorization_ref)
        return "客廳窗簾已執行指令：移動至 開啟 50%。", st
    pct_match = re.search(r"(\d{1,3})\s*(?:%|％|成)", text)
    if pct_match:
        raw_val = int(pct_match.group(1))
        if "成" in pct_match.group(0) and raw_val <= 10:
            raw_val *= 10
        target = min(100, raw_val)
        st = move_to(target, repo=repo, source=source, authorization_ref=authorization_ref)
        label = "全開 (100%)" if target >= 99 else "全關 (0%)" if target <= 1 else f"開啟 {target}%"
        return f"客廳窗簾已執行指令：移動至 {label}。", st
    if any(word in text for word in ("全開", "打開", "開啟", "拉開", "開窗簾")):
        st = move_to(100, repo=repo, source=source, authorization_ref=authorization_ref)
        return "客廳窗簾已執行指令：移動至 全開 (100%)。", st
    if any(word in text for word in ("全關", "關閉", "關上", "拉上", "關窗簾", "關掉")):
        st = move_to(0, repo=repo, source=source, authorization_ref=authorization_ref)
        return "客廳窗簾已執行指令：移動至 全關 (0%)。", st
    raise ValueError("無法辨識窗簾指令")


def execute_command(
    command: str,
    *,
    repo: Any = None,
    source: str = "rest_api",
    authorization_ref: str = "direct",
) -> dict[str, Any]:
    _msg, st = execute_command_with_summary(
        command,
        repo=repo,
        source=source,
        authorization_ref=authorization_ref,
    )
    return st


class CurtainTools:
    """Device-scoped tools for Curtain Agent (`curtain-1` only)."""

    ALLOWED_TOOLS = {
        "get_curtain_context",
        "set_curtain_position",
        "stop_curtain_movement",
        "schedule_curtain",
        "delete_curtain_schedule",
        "set_curtain_schedule_enabled",
    }

    def __init__(self, repo, root_id: str, original_text: str):
        self.repo = repo
        self.root_id = root_id
        self.original_text = original_text

    def get_curtain_context(self) -> dict[str, object]:
        current_schedules = list_schedules_data(self.repo)
        logical_now = _logical_now(self.repo).isoformat() if self.repo is not None else datetime.now().astimezone().isoformat()
        clock_speed = self.repo.clock().speed if self.repo is not None else 1.0
        return {
            "device_id": "curtain-1",
            "state": get_status(repo=self.repo),
            "schedules": current_schedules,
            "logical_now": logical_now,
            "clock_speed": clock_speed,
            "position_meaning": "0% 為全關（遮光），50% 為半開，100% 為全開（完全拉開透光）",
            "scope": "只能依使用者需求控制客廳智慧窗簾 curtain-1 與管理其每日排程",
        }

    def set_curtain_position(
        self,
        device_id: str = "curtain-1",
        position: float = 100.0,
        duration_seconds: float = 2.0,
    ) -> dict[str, object]:
        if device_id != "curtain-1":
            raise ValueError("Curtain Agent 只能控制 curtain-1")
        if type(position) is bool or not isinstance(position, (int, float)) or not 0 <= float(position) <= 100:
            raise ValueError("窗簾位置須為 0～100 之間的百分比數值")
        if type(duration_seconds) is bool or not isinstance(duration_seconds, (int, float)) or not 0 <= float(duration_seconds) <= 120:
            raise ValueError("移動秒數須為 0～120")
        updated = move_to(
            float(position),
            float(duration_seconds),
            repo=self.repo,
            source="Curtain Agent",
            authorization_ref=f"household:{self.root_id}",
        )
        target = round(float(updated["target"]))
        label = "全開 (100%)" if target >= 99 else "全關 (0%)" if target <= 1 else f"開啟 {target}%"
        return {
            "device_id": "curtain-1",
            "state": updated,
            "message": f"客廳窗簾已執行指令：移動至 {label}。",
        }

    def stop_curtain_movement(self, device_id: str = "curtain-1") -> dict[str, object]:
        if device_id != "curtain-1":
            raise ValueError("Curtain Agent 只能控制 curtain-1")
        updated = stop_curtain(
            repo=self.repo,
            source="Curtain Agent",
            authorization_ref=f"household:{self.root_id}",
        )
        pos = round(float(updated["position"]))
        return {
            "device_id": "curtain-1",
            "state": updated,
            "message": f"客廳窗簾已停止移動，目前位置 {pos}%。",
        }

    def schedule_curtain(
        self,
        device_id: str = "curtain-1",
        time: str = "07:00",
        position: float = 100.0,
        enabled: bool = True,
    ) -> dict[str, object]:
        if device_id != "curtain-1":
            raise ValueError("Curtain Agent 只能控制 curtain-1")
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
            raise ValueError("排程時間格式須為 HH:MM（24 小時制）")
        if type(position) is bool or not isinstance(position, (int, float)) or not 0 <= float(position) <= 100:
            raise ValueError("排程窗簾位置須為 0～100")
        created = create_schedule_entry(
            time,
            float(position),
            enabled=bool(enabled),
            repo=self.repo,
            source="Curtain Agent",
            authorization_ref=f"household:{self.root_id}",
        )
        target = round(float(position))
        return {
            "device_id": "curtain-1",
            "schedule": created,
            "message": f"已為客廳窗簾新增每日 {time} 排程（目標位置 {target}%）。",
        }

    def delete_curtain_schedule(
        self,
        device_id: str = "curtain-1",
        schedule_id: str | None = None,
        time: str | None = None,
        delete_all: bool = False,
    ) -> dict[str, object]:
        if device_id != "curtain-1":
            raise ValueError("Curtain Agent 只能控制 curtain-1")
        if type(delete_all) is not bool:
            raise ValueError("delete_all 須為布林值")
        try:
            removed = delete_schedule_entry(
                schedule_id=schedule_id,
                time_str=time,
                delete_all=delete_all,
                repo=self.repo,
                source="Curtain Agent",
                authorization_ref=f"household:{self.root_id}",
            )
        except KeyError as err:
            raise ValueError(str(err).strip("'\"")) from err
        times = "、".join(item["time"] for item in removed)
        return {
            "device_id": "curtain-1",
            "deleted": removed,
            "schedules": list_schedules_data(self.repo),
            "message": f"已刪除客廳窗簾排程（{times}）。",
        }

    def set_curtain_schedule_enabled(
        self,
        device_id: str = "curtain-1",
        enabled: bool | None = False,
        schedule_id: str | None = None,
        time: str | None = None,
    ) -> dict[str, object]:
        if device_id != "curtain-1":
            raise ValueError("Curtain Agent 只能控制 curtain-1")
        try:
            updated = set_schedule_enabled_entry(
                schedule_id=schedule_id,
                time_str=time,
                enabled=enabled,
                repo=self.repo,
                source="Curtain Agent",
                authorization_ref=f"household:{self.root_id}",
            )
        except KeyError as err:
            raise ValueError(str(err).strip("'\"")) from err
        times = "、".join(item["time"] for item in updated)
        state_label = "啟用" if updated[0]["enabled"] else "停用"
        return {
            "device_id": "curtain-1",
            "updated": updated,
            "schedules": list_schedules_data(self.repo),
            "message": f"已{state_label}客廳窗簾排程（{times}）。",
        }

    def call(self, name: str, arguments: dict) -> dict[str, object]:
        if not isinstance(arguments, dict):
            raise ValueError("工具參數須為物件")
        if name not in self.ALLOWED_TOOLS:
            raise ValueError("Curtain Agent 工具不允許控制其他設備")
        method = getattr(self, name)
        inspect.signature(method).bind(**arguments)
        return method(**arguments)


CURTAIN_SYSTEM = """你是 Curtain Agent，專門負責控制客廳智慧窗簾 curtain-1。使用繁體中文，每回合只回傳一個 JSON 物件。
系統已將目前窗簾狀態、邏輯時間與排程清單放在使用者訊息的 tool_result 欄位，不需重複呼叫 get_curtain_context。
位置定義：0% 代表全關（遮擋陽光／保護隱私），50% 代表半開，100% 代表全開（完全拉開讓陽光照進來）。
可根據使用者情境需求推理目標位置與排程操作：
- 例如「打開窗簾」、「全開」、「讓陽光進來」、「房間太暗了想透光」：呼叫 set_curtain_position 設定 position=100（若說「開一點點／微開」可設 30；「開一半／半開」設 50）。
- 例如「關窗簾」、「拉上窗簾」、「外面太陽太刺眼」、「想睡覺／看電影要遮光」：呼叫 set_curtain_position 設定 position=0。
- 例如「把窗簾再開大一點」：依目前 position 往上增加 25～30（最高 100）；「關小一點」則減少 25～30（最低 0）。
- 例如「停止窗簾」：呼叫 stop_curtain_movement。
- 例如「設定早上 7 點打開窗簾」：呼叫 schedule_curtain 設定 time="07:00", position=100。
- 例如「刪除早上 7 點的窗簾排程」或「刪除全部排程」：呼叫 delete_curtain_schedule（依 schedule_id、time 或 delete_all=true）。
- 例如「停用 07:00 的窗簾排程」或「啟用 07:00 的窗簾排程」：呼叫 set_curtain_schedule_enabled（依 schedule_id 或 time，並設定 enabled=false 或 true）。

工具呼叫格式：
1. 調整位置：{"tool":"set_curtain_position","arguments":{"device_id":"curtain-1","position":100,"duration_seconds":2.0},"summary":"簡短摘要"}
2. 停止移動：{"tool":"stop_curtain_movement","arguments":{"device_id":"curtain-1"},"summary":"停止窗簾"}
3. 新增排程：{"tool":"schedule_curtain","arguments":{"device_id":"curtain-1","time":"07:00","position":100,"enabled":true},"summary":"新增窗簾排程"}
4. 刪除排程：{"tool":"delete_curtain_schedule","arguments":{"device_id":"curtain-1","time":"07:00"},"summary":"刪除窗簾排程"}
5. 啟用／停用排程：{"tool":"set_curtain_schedule_enabled","arguments":{"device_id":"curtain-1","time":"07:00","enabled":false},"summary":"停用窗簾排程"}
若僅查詢窗簾狀態或排程清單，可直接回傳：{"finish":"目前窗簾狀態與排程說明"}
若指令不明確（例如有多筆排程但未指定哪一筆），回傳：{"ask":"具體問題"}
不得控制 curtain-1 以外的任何設備，不得回傳 Markdown 或 JSON 以外的文字。"""

_COMPLETING_TOOLS = {
    "set_curtain_position",
    "stop_curtain_movement",
    "schedule_curtain",
    "delete_curtain_schedule",
    "set_curtain_schedule_enabled",
}


async def curtain_session(repo, event, budget, parent_id, root_id, progress=None):
    """Run Curtain Agent using the configured LLM provider with deterministic fallback when no model is configured."""
    from smart_home_energy.agent import ProviderConfigurationError, ProviderUnavailable
    from smart_home_energy.mcp_server import MCPToolExecutionError, SmartHomeMCPServer
    from smart_home_energy.repository import AgentRun

    started = budget.monotonic()
    before = budget.count
    run_id = uuid4().hex
    original_text = event.payload.get("text", "")
    tools = CurtainTools(repo, root_id, original_text)
    mcp = SmartHomeMCPServer(repo)
    status = "failed"
    message = "窗簾指令未完成"
    provider_name = "none"

    if progress:
        progress("Curtain Agent 正在分析窗簾控制需求")

    def _run_deterministic(provider_label: str, summary_label: str) -> tuple[str, dict[str, Any]]:
        msg, st = execute_command_with_summary(
            original_text,
            repo=repo,
            source="Curtain Agent",
            authorization_ref=f"household:{root_id}",
        )
        repo.log_tool(
            run_id,
            {
                "protocol": "MCP",
                "mcp_method": "tools/call",
                "role": "Curtain Agent",
                "provider": provider_label,
                "device_id": "curtain-1",
                "action": {
                    "tool": "execute_curtain_command",
                    "arguments": {"command": original_text},
                    "summary": summary_label,
                },
                "result": {"message": msg, "state": st},
            },
        )
        return msg, st

    try:
        context = mcp.call_tool_value("Curtain Agent", "get_curtain_context", {}, tool_instance=tools)
        if not budget.providers:
            provider_name = "deterministic-curtain-agent"
            message, _st = _run_deterministic(provider_name, "無模型環境確定性窗簾控制")
            status = "completed"
        else:
            messages = [
                {"role": "system", "content": CURTAIN_SYSTEM},
                {"role": "user", "content": json.dumps({"text": original_text, "tool_result": context}, ensure_ascii=False)},
            ]
            while True:
                reply = await budget.complete(messages, repo, run_id)
                provider_name = budget.providers[budget.index].name if budget.providers else "none"
                try:
                    if len(reply) > 65536:
                        raise ValueError("模型回覆過大")
                    action = json.loads(reply)
                    if not isinstance(action, dict):
                        raise ValueError("回覆須為 JSON 物件")
                    if set(action) in ({"ask"}, {"finish"}):
                        text_out = action.get("ask", action.get("finish"))
                        if not isinstance(text_out, str) or not text_out.strip():
                            raise ValueError("結果不可為空白")
                        status = "needs_details" if "ask" in action else "completed"
                        message = text_out
                        break
                    if set(action) != {"tool", "arguments", "summary"} or not isinstance(action["summary"], str):
                        raise ValueError("工具動作格式不符")
                    result = mcp.call_tool_value(
                        "Curtain Agent",
                        action["tool"],
                        action["arguments"],
                        tool_instance=tools,
                        run_id=run_id,
                        provider=provider_name,
                        device_id="curtain-1",
                        summary=action["summary"],
                    )
                    messages.extend(
                        [
                            {"role": "assistant", "content": reply},
                            {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)},
                        ]
                    )
                    if action["tool"] in _COMPLETING_TOOLS:
                        status = "completed"
                        message = str(result["message"])
                        break
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                    if not isinstance(error, MCPToolExecutionError):
                        repo.log_tool(run_id, {"protocol": "MCP", "role": "Curtain Agent", "device_id": "curtain-1", "error": str(error)})
                    messages.append({"role": "user", "content": json.dumps({"error": str(error)}, ensure_ascii=False)})
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        try:
            provider_name = "deterministic-curtain-fallback"
            message, _st = _run_deterministic(provider_name, "模型不可用時改採確定性窗簾控制")
            status = "completed"
        except ValueError:
            message = str(error)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        message = str(error)
        status = "needs_details"

    run = AgentRun(
        run_id,
        event.event_id,
        status,
        provider_name,
        budget.count - before,
        budget.monotonic() - started,
        message,
        parent_id,
        "curtain-1",
        "Curtain Agent",
    )
    repo.record_run(run)
    return encode(run)


def evaluate_due_schedules(repo: Any, real: datetime | None = None) -> list[dict[str, Any]]:
    """Evaluate curtain daily schedules against `repo.clock()` logical time (supports 1x–100x demo speeds).

    Uses interval scanning `(last_logical_at, logical_now]` so high clock speeds (e.g. 60x, where
    1 real second advances 60 logical seconds) never skip over a scheduled HH:MM boundary.
    """
    now_real = aware(real or utcnow())
    clock = repo.clock()
    logical_now = clock.now(now_real).astimezone(TAIPEI)

    triggered: list[dict[str, Any]] = []
    with lock:
        load_schedules(repo)
        scheduler_state = repo.meta(SCHEDULER_STATE_KEY)
        if not isinstance(scheduler_state, dict) or scheduler_state.get("clock_id") != clock.clock_id:
            if isinstance(scheduler_state, dict) and scheduler_state.get("clock_id"):
                repo.audit(
                    "curtain_scheduler_clock_reset",
                    {
                        "device_id": "curtain-1",
                        "previous_clock_id": scheduler_state.get("clock_id"),
                        "clock_id": clock.clock_id,
                        "speed": clock.speed,
                        "logical_now": logical_now.isoformat(),
                    },
                )
            repo.set_meta(
                SCHEDULER_STATE_KEY,
                {"clock_id": clock.clock_id, "last_logical_at": logical_now.isoformat()},
            )
            return []

        try:
            last_logical = datetime.fromisoformat(str(scheduler_state["last_logical_at"])).astimezone(TAIPEI)
        except (KeyError, ValueError, TypeError):
            repo.set_meta(
                SCHEDULER_STATE_KEY,
                {"clock_id": clock.clock_id, "last_logical_at": logical_now.isoformat()},
            )
            return []

        if logical_now <= last_logical:
            return []

        # Collect candidate calendar dates covered by (last_logical, logical_now] (capped to 2 days).
        start_date = max(last_logical.date(), logical_now.date() - timedelta(days=1))
        days: list[Any] = []
        cursor_date = start_date
        while cursor_date <= logical_now.date():
            days.append(cursor_date)
            cursor_date += timedelta(days=1)

        due_entries: list[tuple[datetime, str, dict[str, Any]]] = []
        for item in schedules.values():
            if not item.get("enabled"):
                continue
            hour_s, minute_s = item["time"].split(":")
            hour, minute = int(hour_s), int(minute_s)
            for day in days:
                day_iso = day.isoformat()
                if item.get("last_run_date") == day_iso:
                    continue
                scheduled_dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TAIPEI)
                if last_logical < scheduled_dt <= logical_now:
                    due_entries.append((scheduled_dt, day_iso, item))

        due_entries.sort(key=lambda entry: entry[0])
        for scheduled_dt, day_iso, item in due_entries:
            previous_run = item.get("last_run_date")
            item["last_run_date"] = day_iso
            try:
                save_schedules(repo)
            except OSError:
                item["last_run_date"] = previous_run
                continue
            move_to(
                item["position"],
                repo=repo,
                source="curtain_schedule",
                authorization_ref=f"schedule:{item['id']}",
                action_name="schedule_trigger",
                extra_audit={
                    "schedule_id": item["id"],
                    "scheduled_time": item["time"],
                    "scheduled_for": scheduled_dt.isoformat(),
                    "clock_id": clock.clock_id,
                    "clock_speed": clock.speed,
                },
                real=now_real,
            )
            repo.audit(
                "curtain_schedule_triggered",
                {
                    "device_id": "curtain-1",
                    "schedule_id": item["id"],
                    "time": item["time"],
                    "position": item["position"],
                    "scheduled_for": scheduled_dt.isoformat(),
                    "logical_now": logical_now.isoformat(),
                    "clock_id": clock.clock_id,
                    "clock_speed": clock.speed,
                },
            )
            triggered.append(dict(item))

        repo.set_meta(
            SCHEDULER_STATE_KEY,
            {"clock_id": clock.clock_id, "last_logical_at": logical_now.isoformat()},
        )
    return triggered


def scheduler_loop():
    """Fallback standalone scheduler loop when curtain.py is run independently without control.py."""
    while not stop_event.is_set():
        now = datetime.now().astimezone()
        minute = now.strftime("%H:%M")
        today = now.date().isoformat()
        with lock:
            for item in schedules.values():
                if not item["enabled"] or item["time"] != minute:
                    continue
                if item.get("last_run_date") == today:
                    continue
                previous_run = item.get("last_run_date")
                item["last_run_date"] = today
                try:
                    save_schedules()
                except OSError as exc:
                    item["last_run_date"] = previous_run
                    print(f"[Curtain scheduler] 儲存排程執行紀錄失敗：{exc}", flush=True)
                    continue
                move_to(item["position"])
                print(f"[Curtain scheduler] {now.isoformat()} 排程觸發：{item['id']} -> {item['position']}%", flush=True)
        stop_event.wait(1.0)


_scheduler_thread: Thread | None = None


def ensure_scheduler_started() -> None:
    global _scheduler_thread
    with lock:
        if not schedules:
            try:
                load_schedules()
            except RuntimeError:
                pass
        if _scheduler_thread is None or not _scheduler_thread.is_alive():
            stop_event.clear()
            _scheduler_thread = Thread(target=scheduler_loop, name="curtain-scheduler", daemon=True)
            _scheduler_thread.start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_schedules()
    ensure_scheduler_started()
    try:
        yield
    finally:
        stop_event.set()
        if _scheduler_thread is not None:
            _scheduler_thread.join(timeout=3)


class PositionRequest(BaseModel):
    position: float = Field(ge=0, le=100)
    duration_seconds: float = Field(default=2, ge=0, le=120)


class SceneRequest(BaseModel):
    scene: str


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=200)


class ScheduleRequest(BaseModel):
    time: str
    position: float = Field(ge=0, le=100)
    enabled: bool = True


def create_curtain_router(repo: Any = None) -> APIRouter:
    """Create the `/api/curtain` router, optionally bound to the shared SQLite Repository for durable logs."""
    api_router = APIRouter(prefix="/api/curtain", tags=["curtain"])

    @api_router.get("/health")
    def health():
        return {"service": "Virtual Curtain Agent", "status": "online"}

    @api_router.get("/status")
    def curtain_status():
        return get_status(repo=repo)

    @api_router.post("/position")
    def curtain_position(request: PositionRequest):
        return move_to(
            request.position,
            request.duration_seconds,
            repo=repo,
            source="rest_api",
            authorization_ref="rest:/api/curtain/position",
        )

    @api_router.post("/stop")
    def curtain_stop():
        return stop_curtain(
            repo=repo,
            source="rest_api",
            authorization_ref="rest:/api/curtain/stop",
        )

    @api_router.post("/scene")
    def curtain_scene(request: SceneRequest):
        scenes = {"open": 100, "half": 50, "close": 0}
        if request.scene not in scenes:
            raise HTTPException(status_code=400, detail="場景僅支援 open、half、close")
        return move_to(
            scenes[request.scene],
            repo=repo,
            source="rest_api",
            authorization_ref=f"rest:/api/curtain/scene:{request.scene}",
            action_name=f"scene:{request.scene}",
        )

    @api_router.post("/command")
    def curtain_command(request: CommandRequest):
        try:
            return execute_command(
                request.command,
                repo=repo,
                source="rest_api",
                authorization_ref="rest:/api/curtain/command",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @api_router.get("/schedules")
    def list_schedules():
        return list_schedules_data(repo)

    @api_router.post("/schedules")
    def create_schedule(request: ScheduleRequest):
        try:
            return create_schedule_entry(
                request.time,
                request.position,
                enabled=request.enabled,
                repo=repo,
                source="rest_api",
                authorization_ref="rest:/api/curtain/schedules",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @api_router.delete("/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str):
        try:
            delete_schedule_entry(
                schedule_id=schedule_id,
                repo=repo,
                source="rest_api",
                authorization_ref=f"rest:/api/curtain/schedules/{schedule_id}",
            )
            return {"success": True, "message": "排程已刪除"}
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error).strip("'\"")) from error
        except RuntimeError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @api_router.post("/schedules/{schedule_id}/toggle")
    def toggle_schedule(schedule_id: str):
        try:
            updated = set_schedule_enabled_entry(
                schedule_id=schedule_id,
                enabled=None,
                repo=repo,
                source="rest_api",
                authorization_ref=f"rest:/api/curtain/schedules/{schedule_id}/toggle",
            )
            return updated[0]
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error).strip("'\"")) from error
        except RuntimeError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    return api_router


def create_schedule(request: ScheduleRequest):
    """Backward-compatible helper for standalone callers."""
    try:
        return create_schedule_entry(request.time, request.position, enabled=request.enabled)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


router = create_curtain_router(repo=None)

app = FastAPI(
    title="Virtual Curtain Agent",
    description="Independent virtual curtain control API",
    version="1.2.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8502",
        "http://127.0.0.1:8502",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)

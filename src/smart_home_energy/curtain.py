"""Independent virtual curtain API with persistent daily schedules.

Run with:
    python -m uvicorn smart_home_energy.curtain:app --app-dir src --host 127.0.0.1 --port 8001

The virtual device advances when its status is requested. Scheduled movements
are triggered by a background thread while this API process is running.
"""

import inspect
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

lock = RLock()
stop_event = Event()

# Keep curtain data separate from the rest of the smart-home project.
DATA_FILE = Path(__file__).resolve().parents[2] / "artifacts" / "runtime" / "curtain_schedules.json"
STATE_FILE = Path(__file__).resolve().parents[2] / "artifacts" / "runtime" / "curtain_state.json"

state = {
    "device_id": "curtain-1",
    "position": 0.0,
    "target": 0.0,
    "status": "stopped",
    "updated_at": datetime.now().astimezone().isoformat(),
}
schedules: dict[str, dict] = {}
last_tick = monotonic()


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


def load_schedules() -> None:
    with lock:
        schedules.clear()
        if not DATA_FILE.exists():
            return
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
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
                schedules[schedule_id] = {
                    "id": schedule_id,
                    "time": schedule_time,
                    "position": float(position),
                    "enabled": bool(item.get("enabled", True)),
                    "last_run_date": item.get("last_run_date"),
                }
        except (OSError, ValueError, TypeError) as exc:
            # Do not silently overwrite a damaged schedules file.
            raise RuntimeError(f"無法讀取窗簾排程資料：{DATA_FILE}: {exc}") from exc


def save_schedules() -> None:
    """Caller must hold lock; atomic replacement avoids partial JSON files."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    temporary.write_text(
        json.dumps(list(schedules.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(DATA_FILE)


def tick():
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


def move_to(position: float, duration: float = 2.0):
    tick()
    with lock:
        state["target"] = float(position)
        state["duration_seconds"] = float(duration)
        if duration <= 0 or abs(state["target"] - state["position"]) < 0.01:
            state["position"] = state["target"]
            state["status"] = "stopped"
        else:
            state["status"] = "moving"
        state["updated_at"] = datetime.now().astimezone().isoformat()
        _save_state_unlocked()
        return dict(state)


def get_status():
    tick()
    with lock:
        return {**state, "position": round(state["position"], 1), "target": round(state["target"], 1)}


def stop_curtain():
    tick()
    with lock:
        state["target"] = state["position"]
        state["status"] = "stopped"
        state["updated_at"] = datetime.now().astimezone().isoformat()
        _save_state_unlocked()
        return dict(state)


def execute_command(command: str):
    text = command.replace(" ", "")
    # Support schedule creation via natural language, e.g. "排程 07:00 全開"
    time_match = re.search(r"((?:[01]\d|2[0-3]):[0-5]\d)", text)
    if time_match and any(word in text for word in ("排程", "定時", "每天", "預約")):
        target_pos = 100.0
        if any(word in text for word in ("半開", "一半", "50%")):
            target_pos = 50.0
        elif any(word in text for word in ("全關", "關閉", "關上", "拉上", "0%")):
            target_pos = 0.0
        else:
            pct = re.search(r"(\d{1,3})%", text)
            if pct:
                target_pos = float(min(100, int(pct.group(1))))
        create_schedule(ScheduleRequest(time=time_match.group(1), position=target_pos, enabled=True))
        return get_status()
    if any(word in text for word in ("停止", "暫停", "停下")):
        return stop_curtain()
    if any(word in text for word in ("半開", "一半", "半關")):
        return move_to(50)
    pct_match = re.search(r"(\d{1,3})\s*(?:%|％|成)", text)
    if pct_match:
        raw_val = int(pct_match.group(1))
        if "成" in pct_match.group(0) and raw_val <= 10:
            raw_val *= 10
        return move_to(min(100, raw_val))
    if any(word in text for word in ("全開", "打開", "開啟", "拉開", "開窗簾")):
        return move_to(100)
    if any(word in text for word in ("全關", "關閉", "關上", "拉上", "關窗簾", "關掉")):
        return move_to(0)
    raise ValueError("無法辨識窗簾指令")


class CurtainTools:
    """Device-scoped tools for Curtain Agent (`curtain-1` only)."""

    def __init__(self, repo, root_id: str, original_text: str):
        self.repo = repo
        self.root_id = root_id
        self.original_text = original_text

    def get_curtain_context(self) -> dict[str, object]:
        with lock:
            current_schedules = [dict(item) for item in schedules.values()]
        return {
            "device_id": "curtain-1",
            "state": get_status(),
            "schedules": current_schedules,
            "position_meaning": "0% 為全關（遮光），50% 為半開，100% 為全開（完全拉開透光）",
            "scope": "只能依使用者需求控制客廳智慧窗簾 curtain-1",
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
        updated = move_to(float(position), float(duration_seconds))
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
        updated = stop_curtain()
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
        created = create_schedule(ScheduleRequest(time=time, position=float(position), enabled=bool(enabled)))
        target = round(float(position))
        return {
            "device_id": "curtain-1",
            "schedule": created,
            "message": f"已為客廳窗簾新增每日 {time} 排程（目標位置 {target}%）。",
        }

    def call(self, name: str, arguments: dict) -> dict[str, object]:
        if not isinstance(arguments, dict):
            raise ValueError("工具參數須為物件")
        if name not in {"get_curtain_context", "set_curtain_position", "stop_curtain_movement", "schedule_curtain"}:
            raise ValueError("Curtain Agent 工具不允許控制其他設備")
        method = getattr(self, name)
        inspect.signature(method).bind(**arguments)
        return method(**arguments)


CURTAIN_SYSTEM = """你是 Curtain Agent，專門負責控制客廳智慧窗簾 curtain-1。使用繁體中文，每回合只回傳一個 JSON 物件。
系統已將目前窗簾狀態與排程放在使用者訊息的 tool_result 欄位，不需重複呼叫 get_curtain_context。
位置定義：0% 代表全關（遮擋陽光／保護隱私），50% 代表半開，100% 代表全開（完全拉開讓陽光照進來）。
可根據使用者情境需求推理目標位置：
- 例如「打開窗簾」、「全開」、「讓陽光進來」、「房間太暗了想透光」：呼叫 set_curtain_position 設定 position=100（若說「開一點點／微開」可設 30；「開一半／半開」設 50）。
- 例如「關窗簾」、「拉上窗簾」、「外面太陽太刺眼」、「想睡覺／看電影要遮光」：呼叫 set_curtain_position 設定 position=0。
- 例如「把窗簾再開大一點」：依目前 position 往上增加 25～30（最高 100）；「關小一點」則減少 25～30（最低 0）。
- 例如「停止窗簾」：呼叫 stop_curtain_movement。
- 例如「設定早上 7 點打開窗簾」：呼叫 schedule_curtain 設定 time="07:00", position=100。

工具呼叫格式：
1. 調整位置：{"tool":"set_curtain_position","arguments":{"device_id":"curtain-1","position":100,"duration_seconds":2.0},"summary":"簡短摘要"}
2. 停止移動：{"tool":"stop_curtain_movement","arguments":{"device_id":"curtain-1"},"summary":"停止窗簾"}
3. 新增排程：{"tool":"schedule_curtain","arguments":{"device_id":"curtain-1","time":"07:00","position":100,"enabled":true},"summary":"新增窗簾排程"}
若僅查詢窗簾狀態，可直接回傳：{"finish":"目前窗簾狀態說明"}
若指令不明確，回傳：{"ask":"具體問題"}
不得控制 curtain-1 以外的任何設備，不得回傳 Markdown 或 JSON 以外的文字。"""


async def curtain_session(repo, event, budget, parent_id, root_id, progress=None):
    """Run Curtain Agent using the configured LLM provider with deterministic fallback when no model is configured."""
    from smart_home_energy.agent import ProviderUnavailable, ProviderConfigurationError
    from smart_home_energy.domain import encode
    from smart_home_energy.repository import AgentRun

    started = budget.monotonic()
    before = budget.count
    run_id = uuid4().hex
    original_text = event.payload.get("text", "")
    tools = CurtainTools(repo, root_id, original_text)
    status = "failed"
    message = "窗簾指令未完成"
    provider_name = "none"

    if progress:
        progress("Curtain Agent 正在分析窗簾控制需求")

    def _deterministic_curtain_fallback(text_cmd: str) -> tuple[str, dict]:
        st = execute_command(text_cmd)
        target = round(float(st.get("target", st.get("position", 0))))
        if st.get("status") == "stopped" and "停" in text_cmd:
            msg = f"客廳窗簾已停止移動，目前位置 {round(float(st.get('position', 0)))}%。"
        elif any(word in text_cmd for word in ("排程", "定時", "每天", "預約")):
            msg = f"已為客廳窗簾設定自動排程（目標位置 {target}%）。"
        else:
            label = "全開 (100%)" if target >= 99 else "全關 (0%)" if target <= 1 else f"開啟 {target}%"
            msg = f"客廳窗簾已執行指令：移動至 {label}。"
        return msg, st

    try:
        context = tools.get_curtain_context()
        if not budget.providers:
            message, st = _deterministic_curtain_fallback(original_text)
            provider_name = "deterministic-curtain-agent"
            status = "completed"
            repo.log_tool(
                run_id,
                {
                    "role": "Curtain Agent",
                    "provider": provider_name,
                    "device_id": "curtain-1",
                    "action": {"tool": "set_curtain_position", "arguments": {"command": original_text}, "summary": "無模型環境確定性窗簾控制"},
                    "result": st,
                },
            )
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
                    result = tools.call(action["tool"], action["arguments"])
                    repo.log_tool(
                        run_id,
                        {
                            "role": "Curtain Agent",
                            "provider": provider_name,
                            "device_id": "curtain-1",
                            "action": action,
                            "result": result,
                        },
                    )
                    messages.extend(
                        [
                            {"role": "assistant", "content": reply},
                            {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)},
                        ]
                    )
                    if action["tool"] in {"set_curtain_position", "stop_curtain_movement", "schedule_curtain"}:
                        status = "completed"
                        message = str(result["message"])
                        break
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                    repo.log_tool(run_id, {"role": "Curtain Agent", "device_id": "curtain-1", "error": str(error)})
                    messages.append({"role": "user", "content": json.dumps({"error": str(error)}, ensure_ascii=False)})
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        try:
            message, _st = _deterministic_curtain_fallback(original_text)
            provider_name = "deterministic-curtain-fallback"
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


def scheduler_loop():
    """Check local HH:MM every second; run each schedule at most once per day."""
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
                # Mark and persist before issuing the command to prevent repeats.
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


router = APIRouter(prefix="/api/curtain", tags=["curtain"])


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


@router.get("/health")
def health():
    return {"service": "Virtual Curtain Agent", "status": "online"}


@router.get("/status")
def curtain_status():
    return get_status()


@router.post("/position")
def curtain_position(request: PositionRequest):
    return move_to(request.position, request.duration_seconds)


@router.post("/stop")
def curtain_stop():
    return stop_curtain()


@router.post("/scene")
def curtain_scene(request: SceneRequest):
    scenes = {"open": 100, "half": 50, "close": 0}
    if request.scene not in scenes:
        raise HTTPException(status_code=400, detail="場景僅支援 open、half、close")
    return move_to(scenes[request.scene])


@router.post("/command")
def curtain_command(request: CommandRequest):
    try:
        return execute_command(request.command)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/schedules")
def list_schedules():
    with lock:
        return [dict(item) for item in schedules.values()]


@router.post("/schedules")
def create_schedule(request: ScheduleRequest):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", request.time):
        raise HTTPException(status_code=400, detail="時間格式錯誤，請使用 HH:MM")
    with lock:
        schedule_id = uuid4().hex
        item = {
            "id": schedule_id,
            "time": request.time,
            "position": request.position,
            "enabled": request.enabled,
            "last_run_date": None,
        }
        schedules[schedule_id] = item
        try:
            save_schedules()
        except OSError as exc:
            del schedules[schedule_id]
            raise HTTPException(status_code=500, detail=f"排程儲存失敗：{exc}") from exc
        return dict(item)


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    with lock:
        if schedule_id not in schedules:
            raise HTTPException(status_code=404, detail="找不到排程")
        previous = schedules.pop(schedule_id)
        try:
            save_schedules()
        except OSError as exc:
            schedules[schedule_id] = previous
            raise HTTPException(status_code=500, detail=f"排程儲存失敗：{exc}") from exc
        return {"success": True, "message": "排程已刪除"}


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: str):
    with lock:
        item = schedules.get(schedule_id)
        if item is None:
            raise HTTPException(status_code=404, detail="找不到排程")
        item["enabled"] = not item["enabled"]
        try:
            save_schedules()
        except OSError as exc:
            item["enabled"] = not item["enabled"]
            raise HTTPException(status_code=500, detail=f"排程儲存失敗：{exc}") from exc
        return dict(item)


app = FastAPI(
    title="Virtual Curtain Agent",
    description="Independent virtual curtain control API",
    version="1.1.0",
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

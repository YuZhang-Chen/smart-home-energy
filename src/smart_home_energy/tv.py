"""Virtual TV control API and natural-language command parser."""

from datetime import datetime
import inspect
import json
from pathlib import Path
import re
from threading import RLock
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

lock = RLock()
DATA_FILE = Path(__file__).resolve().parents[2] / "artifacts" / "runtime" / "tv_state.json"

InputSource = Literal["HDMI1", "HDMI2", "HDMI3", "HDMI4"]
PlaybackState = Literal["playing", "paused", "stopped"]

state: dict[str, object] = {
    "device_id": "tv-1",
    "power": False,
    "volume": 25,
    "muted": False,
    "input": "HDMI1",
    "playback": "stopped",
    "updated_at": datetime.now().astimezone().isoformat(),
}


def _load_state_unlocked() -> None:
    if not DATA_FILE.exists():
        return
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in ("power", "volume", "muted", "input", "playback", "updated_at"):
                if key in raw:
                    state[key] = raw[key]
    except (OSError, ValueError, TypeError):
        pass


def _save_state_unlocked() -> None:
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temp.replace(DATA_FILE)
    except OSError:
        pass


def get_status() -> dict[str, object]:
    with lock:
        _load_state_unlocked()
        return dict(state)


def update_tv(
    *,
    power: bool | None = None,
    volume: int | None = None,
    muted: bool | None = None,
    input_source: InputSource | None = None,
    playback: PlaybackState | None = None,
) -> dict[str, object]:
    with lock:
        _load_state_unlocked()
        if power is not None:
            state["power"] = bool(power)
            if not state["power"]:
                state["playback"] = "stopped"
        if volume is not None:
            state["volume"] = max(0, min(100, int(volume)))
            if muted is None:
                state["muted"] = False
        if muted is not None:
            state["muted"] = bool(muted)
        if input_source is not None:
            if input_source not in {"HDMI1", "HDMI2", "HDMI3", "HDMI4"}:
                raise ValueError("訊號來源僅支援 HDMI1 至 HDMI4")
            state["input"] = input_source
        if playback is not None:
            if playback not in {"playing", "paused", "stopped"}:
                raise ValueError("播放狀態僅支援 playing、paused、stopped")
            state["playback"] = playback
        state["updated_at"] = datetime.now().astimezone().isoformat()
        _save_state_unlocked()
        return dict(state)


def execute_command(command: str) -> dict[str, object]:
    text = command.strip()
    if not text:
        raise ValueError("請輸入電視控制指令")
    normalized = text.lower()
    actions: list[str] = []

    with lock:
        if re.search(r"關閉電視|關掉電視|電視關機|關電視|關機|turn\s*off", normalized):
            update_tv(power=False)
            return {
                "status": get_status(),
                "message": "已關閉客廳電視，進入待機狀態。",
            }

        if re.search(r"打開電視|開啟電視|電視開機|開電視|開機|turn\s*on", normalized):
            update_tv(power=True)
            actions.append("開啟電視")

        hdmi_match = re.search(r"hdmi\s*[-_#]?\s*([1-4])", normalized, re.I) or re.search(
            r"切換(?:到|至)?\s*(?:訊號源|輸入源|hdmi)?\s*([1-4])", normalized, re.I
        )
        if hdmi_match:
            source = f"HDMI{hdmi_match.group(1)}"
            if not state["power"]:
                update_tv(power=True)
                actions.append("開啟電視")
            update_tv(input_source=source)  # type: ignore[arg-type]
            actions.append(f"切換至 {source}")

        # Strip out HDMI numbers before checking volume numbers.
        volume_text = re.sub(r"hdmi\s*[-_#]?\s*[1-4]", " ", normalized, flags=re.I)
        if re.search(r"音量|聲音|大小聲|調大|調小|調到|調至|設為|設成", volume_text):
            vol_match = re.search(r"(?<!\d)(\d{1,3})(?!\d)", volume_text)
            if vol_match:
                vol = int(vol_match.group(1))
                if not 0 <= vol <= 100:
                    raise ValueError("電視音量範圍為 0～100")
                if not state["power"]:
                    update_tv(power=True)
                    actions.append("開啟電視")
                update_tv(volume=vol, muted=False)
                actions.append(f"音量調整至 {vol}%")
            elif re.search(r"大聲一點|調大|增加音量|音量調高", volume_text):
                if not state["power"]:
                    update_tv(power=True)
                    actions.append("開啟電視")
                next_vol = min(100, int(state["volume"]) + 5)
                update_tv(volume=next_vol, muted=False)
                actions.append(f"音量調高至 {next_vol}%")
            elif re.search(r"小聲一點|調小|降低音量|音量調低", volume_text):
                if not state["power"]:
                    update_tv(power=True)
                    actions.append("開啟電視")
                next_vol = max(0, int(state["volume"]) - 5)
                update_tv(volume=next_vol, muted=False)
                actions.append(f"音量調低至 {next_vol}%")

        if re.search(r"取消靜音|解除靜音|打開聲音|開啟聲音", normalized):
            update_tv(muted=False)
            actions.append("解除靜音")
        elif re.search(r"靜音|閉嘴|關聲音", normalized):
            update_tv(muted=True)
            actions.append("開啟靜音")

        if re.search(r"暫停播放|暫停", normalized):
            if not state["power"]:
                raise ValueError("請先開啟電視，再控制播放")
            update_tv(playback="paused")
            actions.append("暫停播放")
        elif re.search(r"開始播放|繼續播放|播放|繼續", normalized):
            if not state["power"]:
                update_tv(power=True)
                actions.append("開啟電視")
            update_tv(playback="playing")
            actions.append("開始播放")
        elif re.search(r"停止播放|停止", normalized):
            if not state["power"]:
                raise ValueError("請先開啟電視，再控制播放")
            update_tv(playback="stopped")
            actions.append("停止播放")

        if not actions:
            if re.search(r"狀態|如何|幾度|開著嗎|有沒有開", normalized):
                current = get_status()
                power_label = "開啟（運作中）" if current["power"] else "關閉（待機中）"
                vol_label = "靜音" if current["muted"] else f"{current['volume']}%"
                return {
                    "status": current,
                    "message": f"客廳電視目前為{power_label}，訊號源 {current['input']}，音量 {vol_label}。",
                }
            raise ValueError("無法辨識電視指令，可嘗試：打開電視、音量調到 30、切換 HDMI 2、靜音")

        return {
            "status": get_status(),
            "message": "客廳電視已執行：" + "、".join(dict.fromkeys(actions)) + "。",
        }


class TVTools:
    """Device-scoped tools for TV Agent (`tv-1` only)."""

    def __init__(self, repo, root_id: str, original_text: str):
        self.repo = repo
        self.root_id = root_id
        self.original_text = original_text

    def get_tv_context(self) -> dict[str, object]:
        return {
            "device_id": "tv-1",
            "state": get_status(),
            "supported_inputs": ["HDMI1", "HDMI2", "HDMI3", "HDMI4"],
            "supported_playback": ["playing", "paused", "stopped"],
            "volume_range": [0, 100],
            "scope": "只能依使用者需求控制客廳智慧電視 tv-1",
        }

    def control_tv(
        self,
        device_id: str = "tv-1",
        power: bool | None = None,
        volume: int | None = None,
        muted: bool | None = None,
        input: InputSource | None = None,
        playback: PlaybackState | None = None,
    ) -> dict[str, object]:
        if device_id != "tv-1":
            raise ValueError("TV Agent 只能控制 tv-1")
        if all(val is None for val in (power, volume, muted, input, playback)):
            raise ValueError("control_tv 至少需指定一項電視控制參數")
        if power is not None and type(power) is not bool:
            raise ValueError("power 須為布林值")
        if muted is not None and type(muted) is not bool:
            raise ValueError("muted 須為布林值")
        if volume is not None:
            if type(volume) is bool or not isinstance(volume, (int, float)) or not 0 <= int(volume) <= 100:
                raise ValueError("電視音量範圍為 0～100")
            volume = int(volume)
        if input is not None and input not in {"HDMI1", "HDMI2", "HDMI3", "HDMI4"}:
            raise ValueError("訊號來源僅支援 HDMI1 至 HDMI4")
        if playback is not None and playback not in {"playing", "paused", "stopped"}:
            raise ValueError("播放狀態僅支援 playing、paused、stopped")

        current = get_status()
        effective_power = power
        # Automatically turn on the TV when the user requests active media/input/volume changes
        if effective_power is None and not current["power"] and (
            input is not None or volume is not None or playback == "playing"
        ):
            effective_power = True

        updated = update_tv(
            power=effective_power,
            volume=volume,
            muted=muted,
            input_source=input,
            playback=playback,
        )
        changes: list[str] = []
        if effective_power is True and not current["power"]:
            changes.append("開啟電視")
        elif effective_power is False:
            changes.append("關閉電視（進入待機）")
        if input is not None:
            changes.append(f"切換至 {input}")
        if volume is not None:
            changes.append(f"音量設為 {volume}%")
        if muted is True:
            changes.append("開啟靜音")
        elif muted is False and power is None and volume is None:
            changes.append("解除靜音")
        if playback is not None and updated["power"]:
            play_labels = {"playing": "開始播放", "paused": "暫停播放", "stopped": "停止播放"}
            changes.append(play_labels[playback])

        summary_msg = (
            "客廳電視已執行：" + "、".join(changes) + "。"
            if changes
            else f"客廳電視狀態已更新（電源：{'開啟' if updated['power'] else '待機'}，訊號源：{updated['input']}，音量：{updated['volume']}%）。"
        )
        return {
            "device_id": "tv-1",
            "status": updated,
            "message": summary_msg,
        }

    def call(self, name: str, arguments: dict) -> dict[str, object]:
        if not isinstance(arguments, dict):
            raise ValueError("工具參數須為物件")
        if name not in {"get_tv_context", "control_tv"}:
            raise ValueError("TV Agent 工具不允許控制其他設備")
        method = getattr(self, name)
        inspect.signature(method).bind(**arguments)
        return method(**arguments)


TV_SYSTEM = """你是 TV Agent，專門負責控制客廳智慧電視 tv-1。使用繁體中文，每回合只回傳一個 JSON 物件。
系統已將目前電視狀態放在使用者訊息的 tool_result 欄位，不需重複呼叫 get_tv_context。
可根據使用者情境需求推理目標狀態：
- 例如「打開電視」、「想看電影／節目」：power=true（若提到開始看／播放則 playback="playing"）。
- 例如「太吵了」、「聲音小一點」：依目前狀態的 volume 往下降 15～20（最低 0）；「大聲一點」則往上升 15～20（最高 100）。
- 例如「切到遊戲機／第二個 HDMI」：input="HDMI2"（支援 HDMI1、HDMI2、HDMI3、HDMI4）。
- 例如「關閉電視」、「不看了」：power=false。

工具呼叫格式：
{"tool":"control_tv","arguments":{"device_id":"tv-1","power":true,"volume":30,"muted":false,"input":"HDMI2","playback":"playing"},"summary":"簡短摘要"}
（不需要修改的欄位請設為 null 或省略）
若僅查詢電視狀態，可直接回傳：{"finish":"目前電視狀態說明"}
若指令完全無法理解或資訊不足，回傳：{"ask":"具體問題"}
不得控制 tv-1 以外的任何設備，不得回傳 Markdown 或 JSON 以外的文字。"""


async def tv_session(repo, event, budget, parent_id, root_id, progress=None):
    """Run TV Agent using the configured LLM provider with deterministic fallback when no model is configured."""
    from smart_home_energy.agent import ProviderUnavailable, ProviderConfigurationError
    from smart_home_energy.domain import encode
    from smart_home_energy.mcp_server import MCPToolExecutionError, SmartHomeMCPServer
    from smart_home_energy.repository import AgentRun

    started = budget.monotonic()
    before = budget.count
    run_id = uuid4().hex
    original_text = event.payload.get("text", "")
    tools = TVTools(repo, root_id, original_text)
    mcp = SmartHomeMCPServer(repo)
    status = "failed"
    message = "電視指令未完成"
    provider_name = "none"

    if progress:
        progress("TV Agent 正在分析電視控制需求")

    try:
        context = mcp.call_tool_value("TV Agent", "get_tv_context", {}, tool_instance=tools)
        if not budget.providers:
            # Offline / model-free unit test fallback
            outcome = execute_command(original_text)
            provider_name = "deterministic-tv-agent"
            status = "completed"
            message = str(outcome["message"])
            repo.log_tool(
                run_id,
                {
                    "protocol": "MCP",
                    "mcp_method": "tools/call",
                    "role": "TV Agent",
                    "provider": provider_name,
                    "device_id": "tv-1",
                    "action": {"tool": "control_tv", "arguments": {"command": original_text}, "summary": "無模型環境確定性電視控制"},
                    "result": outcome["status"],
                },
            )
        else:
            messages = [
                {"role": "system", "content": TV_SYSTEM},
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
                        "TV Agent",
                        action["tool"],
                        action["arguments"],
                        tool_instance=tools,
                        run_id=run_id,
                        provider=provider_name,
                        device_id="tv-1",
                        summary=action["summary"],
                    )
                    messages.extend(
                        [
                            {"role": "assistant", "content": reply},
                            {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)},
                        ]
                    )
                    if action["tool"] == "control_tv":
                        status = "completed"
                        message = str(result["message"])
                        break
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                    if not isinstance(error, MCPToolExecutionError):
                        repo.log_tool(run_id, {"protocol": "MCP", "role": "TV Agent", "device_id": "tv-1", "error": str(error)})
                    messages.append({"role": "user", "content": json.dumps({"error": str(error)}, ensure_ascii=False)})
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        try:
            outcome = execute_command(original_text)
            provider_name = "deterministic-tv-fallback"
            status = "completed"
            message = str(outcome["message"])
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
        "tv-1",
        "TV Agent",
    )
    repo.record_run(run)
    return encode(run)


router = APIRouter(prefix="/api/tv", tags=["tv"])


class TVUpdateRequest(BaseModel):
    power: bool | None = None
    volume: int | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None
    input: InputSource | None = None
    playback: PlaybackState | None = None


class TVCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=200)


@router.get("/status")
def tv_status():
    return get_status()


@router.post("/control")
def tv_control(request: TVUpdateRequest):
    try:
        return update_tv(
            power=request.power,
            volume=request.volume,
            muted=request.muted,
            input_source=request.input,
            playback=request.playback,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/command")
def tv_command(request: TVCommandRequest):
    try:
        return execute_command(request.command)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

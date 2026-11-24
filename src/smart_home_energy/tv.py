"""Virtual TV control API and natural-language command parser."""

from datetime import datetime
import json
from pathlib import Path
import re
from threading import RLock
from typing import Literal

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

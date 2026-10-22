"""Validated protocol and unit conversions for the virtual color-temperature bulb."""

from dataclasses import dataclass
from datetime import datetime
import math

from smart_home_energy.domain import aware


BULB_ID = "bulb-1"
MIN_MIRED = 154  # approximately 6500 K
MAX_MIRED = 370  # approximately 2700 K


def percent_to_level(percent: float) -> int:
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 < percent <= 100:
        raise ValueError("燈泡亮度須介於 0 到 100%；0% 請使用關燈")
    return max(1, min(254, round(percent * 254 / 100)))


def level_to_percent(level: int) -> int:
    if type(level) is not int or not 1 <= level <= 254:
        raise ValueError("Matter 亮度須介於 1–254")
    return round(level * 100 / 254)


def kelvin_to_mired(kelvin: float) -> int:
    if type(kelvin) not in (int, float) or not math.isfinite(kelvin) or not 2700 <= kelvin <= 6500:
        raise ValueError("燈泡色溫須介於 2700–6500K")
    return max(MIN_MIRED, min(MAX_MIRED, round(1_000_000 / kelvin)))


def mired_to_kelvin(mired: int) -> int:
    if type(mired) is not int or not MIN_MIRED <= mired <= MAX_MIRED:
        raise ValueError("Matter 色溫超出燈泡能力")
    return round(1_000_000 / mired)


@dataclass(frozen=True)
class BulbCommand:
    command_id: str
    action_id: str
    action: str
    expected_boot: str
    expected_state_version: int
    generation: int
    expires_at: datetime
    authorization_ref: str
    power: bool | None = None
    level: int | None = None
    mired: int | None = None
    duration_seconds: float = 0.5
    device_id: str = BULB_ID
    protocol_version: int = 2

    def __post_init__(self):
        aware(self.expires_at)
        if self.device_id != BULB_ID or self.protocol_version != 2 or self.action not in {"set", "query"}:
            raise ValueError("不支援的燈泡命令")
        if not self.command_id or not self.action_id or not self.expected_boot or not self.authorization_ref:
            raise ValueError("燈泡命令缺少識別碼或授權參照")
        if type(self.expected_state_version) is not int or self.expected_state_version < 0:
            raise ValueError("燈泡狀態版本不合法")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("燈泡動作世代不合法")
        if self.power is not None and type(self.power) is not bool:
            raise ValueError("燈泡開關須為布林值")
        if self.level is not None and (type(self.level) is not int or not 1 <= self.level <= 254):
            raise ValueError("Matter 亮度須介於 1–254")
        if self.mired is not None and (type(self.mired) is not int or not MIN_MIRED <= self.mired <= MAX_MIRED):
            raise ValueError("Matter 色溫超出燈泡能力")
        if type(self.duration_seconds) not in (int, float) or not math.isfinite(self.duration_seconds) or not 0 <= self.duration_seconds <= 10:
            raise ValueError("燈泡漸變須介於 0–10 秒")
        if self.action == "set" and self.power is None and self.level is None and self.mired is None:
            raise ValueError("燈泡命令缺少目標")
        if self.action == "query" and any(v is not None for v in (self.power, self.level, self.mired)):
            raise ValueError("查詢命令不能修改燈泡")


@dataclass(frozen=True)
class BulbResult:
    command_id: str
    action_id: str
    accepted: bool
    message: str
    boot_id: str
    state_version: int
    device_id: str = BULB_ID
    protocol_version: int = 2

    def __post_init__(self):
        if self.device_id != BULB_ID or self.protocol_version != 2 or type(self.accepted) is not bool:
            raise ValueError("燈泡回覆不合法")


@dataclass(frozen=True)
class BulbState:
    boot_id: str
    sequence: int
    observed_at: datetime
    state_version: int = 0
    generation: int = 0
    power: bool = False
    level: int = 254
    mired: int = MAX_MIRED
    transitioning: bool = False
    last_command_id: str | None = None
    action_id: str | None = None
    fault: str | None = None
    device_id: str = BULB_ID
    protocol_version: int = 2

    def __post_init__(self):
        aware(self.observed_at)
        if self.device_id != BULB_ID or self.protocol_version != 2 or not self.boot_id:
            raise ValueError("燈泡狀態識別不合法")
        if any(type(v) is not int or v < 0 for v in (self.sequence, self.state_version, self.generation)):
            raise ValueError("燈泡序號或版本不合法")
        if type(self.power) is not bool or type(self.transitioning) is not bool:
            raise ValueError("燈泡狀態型別不合法")
        level_to_percent(self.level)
        mired_to_kelvin(self.mired)

"""Configuration shared by the publisher and dashboard."""

import os
from dataclasses import dataclass
from pathlib import Path
import math

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True)
class Settings:
    mqtt_host: str
    mqtt_port: int
    mqtt_topic: str

    def __post_init__(self) -> None:
        if not self.mqtt_host.strip():
            raise ValueError("MQTT_HOST 不可為空白")
        if not 1 <= self.mqtt_port <= 65535:
            raise ValueError("MQTT_PORT 必須介於 1–65535")
        if (
            not self.mqtt_topic.strip()
            or any(char in self.mqtt_topic for char in ("+", "#", "\0"))
            or len(self.mqtt_topic.encode("utf-8")) > 65535
        ):
            raise ValueError("MQTT_TOPIC 必須是非空白、不含萬用字元的有效發布主題")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ENV_FILE, override=False)
        required = ("MQTT_HOST", "MQTT_PORT", "MQTT_TOPIC")
        missing = [name for name in required if name not in os.environ]
        if missing:
            raise ValueError(f"缺少設定：{', '.join(missing)}，請檢查專案根目錄的 .env")
        return cls(
            mqtt_host=os.environ["MQTT_HOST"].strip(),
            mqtt_port=int(os.environ["MQTT_PORT"]),
            mqtt_topic=os.environ["MQTT_TOPIC"],
        )


@dataclass(frozen=True)
class HemsSettings:
    database: str
    device_database: str
    mqtt_root: str
    wash_minutes: float
    rinse_minutes: float
    spin_minutes: float
    wash_w: float
    rinse_w: float
    spin_w: float
    peak: float
    offpeak: float
    agent_requests: int
    agent_seconds: float
    request_seconds: float

    @classmethod
    def from_env(cls) -> "HemsSettings":
        load_dotenv(ENV_FILE, override=False)
        root = os.getenv("HEMS_MQTT_ROOT", "home/hems").strip().strip("/")
        if not root or any(c in root for c in ("+", "#", "\0")):
            raise ValueError("HEMS_MQTT_ROOT 必須是有效主題")
        base = ENV_FILE.parent
        values = [float(os.getenv(k, v)) for k, v in (
            ("WASH_MINUTES", "30"), ("RINSE_MINUTES", "15"), ("SPIN_MINUTES", "15"),
            ("WASH_POWER_W", "200"), ("RINSE_POWER_W", "100"), ("SPIN_POWER_W", "400"),
            ("TARIFF_PEAK", "6"), ("TARIFF_OFFPEAK", "2.5"),
        )]
        if any(not math.isfinite(v) or v < 0 for v in values) or any(v <= 0 for v in values[:3]):
            raise ValueError("行程與費率參數必須是有效非負數，階段時間需大於零")
        requests = int(os.getenv("AGENT_MAX_REQUESTS", "15"))
        seconds = float(os.getenv("AGENT_MAX_SECONDS", "120"))
        single = float(os.getenv("AGENT_REQUEST_SECONDS", "20"))
        if not 1 <= requests <= 15 or not 0 < seconds <= 120 or not 0 < single <= 20:
            raise ValueError("Agent 預算不可超過 15 次／120 秒／單次 20 秒")
        database = str((base / os.getenv("HEMS_DB", "artifacts/hems.sqlite3")).resolve())
        device_database = str((base / os.getenv("HEMS_DEVICE_DB", "artifacts/washer.sqlite3")).resolve())
        if database == device_database:
            raise ValueError("控制服務與設備資料庫必須分開")
        return cls(database, device_database, root, *values, requests, seconds, single)

    def program(self):
        from smart_home_energy.domain import WashProgram, WashStage
        return WashProgram(stages=(WashStage("洗滌", self.wash_minutes * 60, self.wash_w),
                                   WashStage("漂洗", self.rinse_minutes * 60, self.rinse_w),
                                   WashStage("脫水", self.spin_minutes * 60, self.spin_w)))

    def tariff(self):
        from smart_home_energy.domain import Tariff
        # Version includes all configurable values; existing tasks retain their snapshot.
        return Tariff(version=f"simulated-8-22-{self.peak}-{self.offpeak}", peak=self.peak, offpeak=self.offpeak)

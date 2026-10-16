"""Configuration shared by the publisher and dashboard."""

import os
from dataclasses import dataclass
from pathlib import Path

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

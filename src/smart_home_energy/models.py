"""The telemetry contract. No network or UI dependencies."""

import json
import math
from dataclasses import dataclass, fields
from datetime import UTC, datetime


@dataclass(frozen=True)
class Telemetry:
    run_id: str
    sequence: int
    timestamp: datetime
    device_id: str
    temperature_c: float
    power_w: float
    energy_kwh: float

    def __post_init__(self) -> None:
        for name in ("run_id", "device_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be a nonnegative integer")
        if not isinstance(self.timestamp, datetime) or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        for name in ("temperature_c", "power_w", "energy_kwh"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if self.power_w < 0 or self.energy_kwh < 0:
            raise ValueError("power_w and energy_kwh must be nonnegative")

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "device_id": self.device_id,
            "temperature_c": self.temperature_c,
            "power_w": self.power_w,
            "energy_kwh": self.energy_kwh,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), allow_nan=False)

    @classmethod
    def from_json(cls, payload: bytes | str) -> "Telemetry":
        data = json.loads(payload)
        expected = {field.name for field in fields(cls)}
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError("telemetry fields do not match the contract")
        if not isinstance(data["timestamp"], str):
            raise ValueError("timestamp must be an ISO 8601 string")
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)

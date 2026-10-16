"""Thread-safe handoff from MQTT's network thread to the dashboard."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from smart_home_energy.models import Telemetry


@dataclass(frozen=True)
class Snapshot:
    connected: bool
    samples: tuple[Telemetry, ...]

    @property
    def latest(self) -> Telemetry | None:
        return self.samples[-1] if self.samples else None

    def is_stale(self, now: datetime) -> bool:
        return self.latest is not None and (now - self.latest.timestamp).total_seconds() > 10


class TelemetryStore:
    def __init__(self, max_samples: int = 900) -> None:
        self._lock = Lock()
        self._connected = False
        self._samples: deque[Telemetry] = deque(maxlen=max_samples)

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def add(self, sample: Telemetry) -> None:
        with self._lock:
            if self._samples:
                latest = self._samples[-1]
                if sample.run_id != latest.run_id:
                    self._samples.clear()
                elif sample.sequence <= latest.sequence:
                    return
            self._samples.append(sample)

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self._connected, tuple(self._samples))

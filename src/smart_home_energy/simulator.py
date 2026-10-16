"""Deterministic teaching model; all time is supplied by the caller."""

import math
from dataclasses import dataclass, replace


def integrate_energy(power_w: float, elapsed_seconds: float) -> float:
    """Return kWh for a constant power over the supplied interval."""
    if not math.isfinite(power_w) or power_w < 0:
        raise ValueError("power_w must be finite and nonnegative")
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must be finite and nonnegative")
    return power_w * elapsed_seconds / 3_600_000


@dataclass(frozen=True)
class AirConditioner:
    temperature_c: float = 30.0
    target_temperature_c: float = 27.0
    energy_kwh: float = 0.0

    @property
    def power_w(self) -> float:
        difference = self.temperature_c - self.target_temperature_c
        return min(1200.0, max(100.0, 100.0 + 300.0 * difference))

    def step(self, elapsed_seconds: float) -> "AirConditioner":
        energy = self.energy_kwh + integrate_energy(self.power_w, elapsed_seconds)
        temperature = self.target_temperature_c + (
            self.temperature_c - self.target_temperature_c
        ) * math.exp(-elapsed_seconds / 600.0)
        return replace(self, temperature_c=temperature, energy_kwh=energy)

"""Offline lighting decisions. No I/O, implicit clocks, or device commands."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, time
from hashlib import sha256
import json

from .domain import TAIPEI, aware, encode, decode
from .bulb_domain import percent_to_level, kelvin_to_mired


@dataclass(frozen=True)
class LightingRule:
    enabled: bool = False
    day_start: int = 360
    night_start: int = 1080
    brightness: float = 40
    kelvin: float = 2700
    presence_enabled: bool = False
    version: int = 1

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.presence_enabled) is not bool:
            raise ValueError("enabled flags must be booleans")
        if any(type(v) is not int or not 0 <= v < 1440 for v in (self.day_start, self.night_start)) or self.day_start == self.night_start:
            raise ValueError("distinct minute boundaries required")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("positive version required")
        percent_to_level(self.brightness)
        kelvin_to_mired(self.kelvin)


@dataclass(frozen=True)
class EnvironmentClock:
    generation: str
    real_base: datetime
    environment_base: datetime
    speed: int = 1

    def __post_init__(self):
        aware(self.real_base)
        aware(self.environment_base)
        if not self.generation or type(self.speed) is not int or self.speed not in (1, 60):
            raise ValueError("generation and speed 1/60 required")

    def now(self, real: datetime) -> datetime:
        return aware(self.environment_base) + (aware(real) - aware(self.real_base)) * self.speed


@dataclass(frozen=True)
class Presence:
    value: str
    observed_at: datetime
    expires_at: datetime
    source: str = "simulated"

    def __post_init__(self):
        if self.value not in ("present", "absent", "unknown") or self.source != "simulated":
            raise ValueError("invalid simulated presence")
        if aware(self.expires_at) <= aware(self.observed_at):
            raise ValueError("expiry must follow observation")

    @classmethod
    def observe(cls, value: str, real: datetime):
        return cls(value, aware(real), aware(real) + timedelta(minutes=15))


@dataclass(frozen=True)
class Environment:
    clock: EnvironmentClock
    real_now: datetime
    presence: Presence | None = None

    def __post_init__(self):
        aware(self.real_now)


@dataclass(frozen=True)
class LightingState:
    generation: str = ""
    absent_since: datetime | None = None
    override_until: datetime | None = None
    last_real: datetime | None = None
    presence_valid_until: datetime | None = None

    def __post_init__(self):
        for value in (self.absent_since, self.override_until, self.last_real, self.presence_valid_until):
            if value is not None:
                aware(value)

    def to_dict(self):
        return encode(self)

    @classmethod
    def from_dict(cls, value):
        return decode(cls, value)


@dataclass(frozen=True)
class LightingTarget:
    power: bool
    level: int | None = None
    mired: int | None = None


@dataclass(frozen=True)
class Evaluation:
    state: LightingState
    reason: str
    window: str
    next_boundary: datetime
    target: LightingTarget | None
    decision_id: str | None


def boundaries(rule, now):
    local = aware(now).astimezone(TAIPEI)
    points = []
    for offset in (-1, 0, 1):
        midnight = datetime.combine(local.date() + timedelta(days=offset), time(), TAIPEI)
        points.extend((midnight + timedelta(minutes=m), name) for m, name in ((rule.day_start, "day"), (rule.night_start, "night")))
    previous, window = max((p for p in points if p[0] <= local), key=lambda p: p[0])
    following = min(p[0] for p in points if p[0] > local)
    return window, aware(previous), aware(following)


def manual_override(rule, environment, state):
    until = boundaries(rule, environment.clock.now(environment.real_now))[2]
    return replace(state, generation=environment.clock.generation, override_until=until)


def resume(state):
    return replace(state, override_until=None, absent_since=None)


def switch_clock(rule, real, environment_time, speed, generation, previous_generation):
    if rule.enabled or generation == previous_generation:
        raise ValueError("disable rule and supply a new generation before switching")
    return EnvironmentClock(generation, real, environment_time, speed), LightingState(generation)


def evaluate(rule: LightingRule, environment: Environment, state: LightingState) -> Evaluation:
    real = aware(environment.real_now)
    now = environment.clock.now(real)
    window, start, following = boundaries(rule, now)
    if state.generation != environment.clock.generation or (state.last_real and real < state.last_real):
        state = LightingState(environment.clock.generation)
    presence = environment.presence
    valid = presence is not None and aware(presence.observed_at) <= real < aware(presence.expires_at)
    # A gap after the prior sample expired breaks continuous absence.
    continuity = valid and state.presence_valid_until is not None and aware(presence.observed_at) <= state.presence_valid_until
    absent = state.absent_since
    if not rule.enabled or not rule.presence_enabled or not valid or presence.value != "absent":
        absent = None
    elif absent is None or not continuity:
        absent = now
    state = replace(state, absent_since=absent, last_real=real, presence_valid_until=presence.expires_at if valid else None)
    reason, target = "disabled", None
    if rule.enabled:
        if state.override_until and now < state.override_until:
            reason = "manual_override"
        elif rule.presence_enabled and (not valid or presence.value == "unknown"):
            reason = "presence_unknown"
        elif window == "day":
            reason, target = "day", LightingTarget(False)
        elif rule.presence_enabled and presence.value == "absent":
            reason = "absence_wait"
            if absent is not None and now - absent >= timedelta(minutes=5):
                reason, target = "absence_elapsed", LightingTarget(False)
        else:
            reason, target = "night", LightingTarget(True, percent_to_level(rule.brightness), kelvin_to_mired(rule.kelvin))
    if state.override_until and now >= state.override_until:
        state = replace(state, override_until=None)
    key = None if target is None else sha256(json.dumps(encode([rule, environment.clock.generation, start, reason, target]), sort_keys=True).encode()).hexdigest()
    return Evaluation(state, reason, window, following, target, key)

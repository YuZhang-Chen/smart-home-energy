"""Deterministic AC comfort plans and energy estimates.

This is a teaching simulator, not a physical HVAC controller.
The cooling curve uses a first-order inverter-style model:
    T(t) = target + (initial - target) * exp(-t / cooling_seconds)
Power is estimated from the temperature error and integrated exactly.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from uuid import uuid4

from smart_home_energy.domain import Tariff, aware, number


MIN_POWER_W = 100.0
MAX_POWER_W = 1200.0
POWER_GAIN_W_PER_C = 300.0
DEFAULT_COOLING_SECONDS = 600.0


@dataclass(frozen=True)
class ACConstraints:
    start: datetime
    end: datetime
    target_c: float = 27
    minimum_c: float = 26
    maximum_c: float = 28
    ambient_c: float = 32
    warming_seconds: float = 1800
    device_id: str = "ac-1"
    # Kept after device_id to preserve the positional order of older callers.
    cooling_seconds: float = DEFAULT_COOLING_SECONDS

    def __post_init__(self):
        if self.device_id != "ac-1" or not 0 < (aware(self.end) - aware(self.start)).total_seconds() <= 86400:
            raise ValueError("冷氣時段須大於零且不超過一天")

        for value in (self.target_c, self.minimum_c, self.maximum_c, self.ambient_c):
            number(value, "溫度", -50)

        number(self.warming_seconds, "回溫時間常數", 1)
        number(self.cooling_seconds, "降溫時間常數", 1)

        if not 16 <= self.minimum_c <= self.target_c <= self.maximum_c <= 30 or self.minimum_c >= self.maximum_c:
            raise ValueError("冷氣目標及舒適範圍須介於 16–30°C")


@dataclass(frozen=True)
class ACPlan:
    plan_id: str
    clock_id: str
    constraints: ACConstraints
    initial_c: float
    energy_kwh: float
    cost: float
    comfortable_after_seconds: float | None
    tariff: Tariff
    start_on_approval: bool = False


@dataclass(frozen=True)
class ACApproval:
    constraints: ACConstraints
    clock_id: str
    tariff: Tariff


def cooling_power_w(temperature: float, target: float) -> float:
    """Estimate inverter power from the current temperature error."""
    number(temperature, "目前室溫", -50)
    number(target, "目標溫度", -50)
    return min(
        MAX_POWER_W,
        max(MIN_POWER_W, MIN_POWER_W + POWER_GAIN_W_PER_C * (temperature - target)),
    )


def cooling_temperature(
    initial: float,
    target: float,
    seconds: float,
    cooling_seconds: float = DEFAULT_COOLING_SECONDS,
) -> float:
    """Return the simulated room temperature after active cooling."""
    number(initial, "初始室溫", -50)
    number(target, "目標溫度", -50)
    number(seconds, "經過秒數", 0)
    number(cooling_seconds, "降溫時間常數", 1)
    return target + (initial - target) * math.exp(-seconds / cooling_seconds)


def cooling_energy(
    initial: float,
    target: float,
    seconds: float,
    cooling_seconds: float = DEFAULT_COOLING_SECONDS,
) -> float:
    """Integrate the estimated inverter power curve and return kWh.

    Power is clamp(100 + 300 * (T - target), 100, 1200) watts.
    The temperature curve is the same first-order curve used by
    ``cooling_temperature`` so predicted energy and displayed temperature
    remain mathematically consistent.
    """
    number(initial, "初始室溫", -50)
    number(target, "目標溫度", -50)
    number(seconds, "經過秒數", 0)
    number(cooling_seconds, "降溫時間常數", 1)

    if seconds == 0:
        return 0.0

    delta = initial - target
    if delta <= 0:
        return MIN_POWER_W * seconds / 3_600_000

    # Time until the estimated power falls below MAX_POWER_W.
    power_delta = (MAX_POWER_W - MIN_POWER_W) / POWER_GAIN_W_PER_C
    clipped = max(0.0, cooling_seconds * math.log(delta / power_delta))
    high = min(seconds, clipped)

    energy_ws = MAX_POWER_W * high
    if seconds > high:
        energy_ws += MIN_POWER_W * (seconds - high)
        energy_ws += (
            POWER_GAIN_W_PER_C
            * delta
            * cooling_seconds
            * (math.exp(-high / cooling_seconds) - math.exp(-seconds / cooling_seconds))
        )

    return energy_ws / 3_600_000


def cooling_segments(c: ACConstraints, initial: float, used: float):
    """Yield minute-aligned average-power segments for tariff calculation."""
    offset = 0.0
    used = max(0.0, used)

    while offset < used:
        at = c.start + timedelta(seconds=offset)
        boundary = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        stop = min(used, offset + (boundary - at).total_seconds())
        segment_seconds = stop - offset

        energy_before = cooling_energy(
            initial, c.target_c, offset, c.cooling_seconds
        )
        energy_after = cooling_energy(
            initial, c.target_c, stop, c.cooling_seconds
        )
        segment_kwh = energy_after - energy_before
        average_power_w = segment_kwh * 3_600_000 / segment_seconds

        yield at, segment_seconds, average_power_w
        offset = stop


def predict(
    c: ACConstraints,
    initial: float,
    tariff: Tariff,
    elapsed: float | None = None,
):
    """Predict temperature, energy and cost at a given elapsed time."""
    duration = (aware(c.end) - aware(c.start)).total_seconds()
    requested_elapsed = duration if elapsed is None else max(0.0, elapsed)
    used = min(duration, requested_elapsed)

    temperature = cooling_temperature(
        initial, c.target_c, used, c.cooling_seconds
    )
    energy = cooling_energy(
        initial, c.target_c, used, c.cooling_seconds
    )

    # Minute segments preserve tariff/calendar boundary splitting.
    cost = tariff.segment_cost(cooling_segments(c, initial, used))
    return temperature, energy, cost


def make_ac_plan(c: ACConstraints, initial: float, tariff: Tariff, clock_id: str) -> ACPlan:
    number(initial, "室溫", -50)

    _, energy, cost = predict(c, initial, tariff)
    comfortable = 0.0 if c.minimum_c <= initial <= c.maximum_c else None

    if initial > c.maximum_c and c.target_c < c.maximum_c:
        comfortable = c.cooling_seconds * math.log(
            (initial - c.target_c) / (c.maximum_c - c.target_c)
        )
    elif initial < c.minimum_c and c.target_c > c.minimum_c:
        comfortable = c.cooling_seconds * math.log(
            (c.target_c - initial) / (c.target_c - c.minimum_c)
        )

    if comfortable is not None and comfortable > (c.end - c.start).total_seconds():
        comfortable = None

    return ACPlan(
        uuid4().hex,
        clock_id,
        c,
        initial,
        energy,
        cost,
        comfortable,
        tariff,
    )


def validate_ac(
    plan: ACPlan,
    now: datetime,
    clock_id: str,
    approval: ACApproval | None = None,
):
    if type(plan.start_on_approval) is not bool:
        raise ValueError("冷氣啟動方式不合法")

    if plan.clock_id != clock_id or (
        plan.constraints.start < aware(now)
        and not (plan.start_on_approval and approval is None)
    ):
        raise ValueError("冷氣計畫時鐘不符或開始時間已過期")

    expected = make_ac_plan(
        plan.constraints, plan.initial_c, plan.tariff, clock_id
    )

    # A valid short test run may end before the room reaches the comfort band.
    # comfortable_after_seconds remains None in that case and is reported as an estimate.

    if (
        not math.isclose(expected.energy_kwh, plan.energy_kwh, abs_tol=1e-10)
        or not math.isclose(expected.cost, plan.cost, abs_tol=1e-10)
        or expected.comfortable_after_seconds != plan.comfortable_after_seconds
    ):
        raise ValueError("冷氣計畫計算不符")

    if approval:
        approved = approval.constraints
        requested = plan.constraints

        if (
            approval.clock_id != clock_id
            or approval.tariff != plan.tariff
            or requested.start < approved.start
            or requested.end > approved.end
            or not (
                approved.minimum_c
                <= requested.minimum_c
                <= requested.target_c
                <= requested.maximum_c
                <= approved.maximum_c
            )
            or requested.ambient_c != approved.ambient_c
            or requested.warming_seconds != approved.warming_seconds
            or requested.cooling_seconds != approved.cooling_seconds
        ):
            raise ValueError("冷氣計畫超出核准時段、溫度或模型參數")

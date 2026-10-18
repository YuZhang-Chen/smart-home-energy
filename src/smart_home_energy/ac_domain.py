"""Deterministic comfort plans; temperatures and costs are teaching estimates."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from uuid import uuid4

from smart_home_energy.domain import Tariff, aware, number


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

    def __post_init__(self):
        if self.device_id != "ac-1" or not 0 < (aware(self.end) - aware(self.start)).total_seconds() <= 86400:
            raise ValueError("冷氣時段須大於零且不超過一天")
        for value in (self.target_c, self.minimum_c, self.maximum_c, self.ambient_c):
            number(value, "溫度", -50)
        number(self.warming_seconds, "回溫時間常數", 1)
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


def cooling_energy(initial: float, target: float, seconds: float) -> float:
    """Exact integral of clamp(100+300*(T-target),100,1200)."""
    number(seconds, "經過秒數")
    delta = initial - target
    if delta <= 0:
        return 100 * seconds / 3_600_000
    clipped = max(0, 600 * math.log(delta / (1100 / 300)))
    high = min(seconds, clipped)
    energy = 1200 * high
    if seconds > high:
        energy += 100 * (seconds - high) + 300 * delta * 600 * (math.exp(-high / 600) - math.exp(-seconds / 600))
    return energy / 3_600_000


def cooling_segments(c: ACConstraints, initial: float, used: float):
    offset = 0.0
    while offset < used:
        at = c.start + timedelta(seconds=offset)
        boundary = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        stop = min(used, offset + (boundary - at).total_seconds())
        energy = cooling_energy(initial, c.target_c, stop) - cooling_energy(initial, c.target_c, offset)
        yield at, stop - offset, energy * 3_600_000 / (stop - offset)
        offset = stop


def predict(c: ACConstraints, initial: float, tariff: Tariff, elapsed: float | None = None):
    duration = (c.end - c.start).total_seconds()
    used = min(duration, max(0, duration if elapsed is None else elapsed))
    temperature = c.target_c + (initial - c.target_c) * math.exp(-used / 600)
    # Minute segments preserve exact energy and allow calendar/tier boundary splits.
    cost = tariff.segment_cost(cooling_segments(c, initial, used))
    return temperature, cooling_energy(initial, c.target_c, used), cost


def make_ac_plan(c: ACConstraints, initial: float, tariff: Tariff, clock_id: str) -> ACPlan:
    number(initial, "室溫", -50)
    _, energy, cost = predict(c, initial, tariff)
    comfortable = 0.0 if c.minimum_c <= initial <= c.maximum_c else None
    if initial > c.maximum_c and c.target_c < c.maximum_c:
        comfortable = 600 * math.log((initial - c.target_c) / (c.maximum_c - c.target_c))
    elif initial < c.minimum_c and c.target_c > c.minimum_c:
        comfortable = 600 * math.log((c.target_c - initial) / (c.target_c - c.minimum_c))
    if comfortable is not None and comfortable > (c.end - c.start).total_seconds():
        comfortable = None
    return ACPlan(uuid4().hex, clock_id, c, initial, energy, cost, comfortable, tariff)


def validate_ac(plan: ACPlan, now: datetime, clock_id: str, approval: ACApproval | None = None):
    if type(plan.start_on_approval) is not bool:
        raise ValueError("冷氣啟動方式不合法")
    if plan.clock_id != clock_id or (plan.constraints.start < aware(now) and not (plan.start_on_approval and approval is None)):
        raise ValueError("冷氣計畫時鐘不符或開始時間已過期")
    expected = make_ac_plan(plan.constraints, plan.initial_c, plan.tariff, clock_id)
    if expected.comfortable_after_seconds is None:
        raise ValueError("指定冷氣時段內無法達到舒適範圍，請調整時段或溫度")
    if not math.isclose(expected.energy_kwh, plan.energy_kwh, abs_tol=1e-10) or not math.isclose(expected.cost, plan.cost, abs_tol=1e-10) or expected.comfortable_after_seconds != plan.comfortable_after_seconds:
        raise ValueError("冷氣計畫計算不符")
    if approval:
        a, c = approval.constraints, plan.constraints
        if approval.clock_id != clock_id or approval.tariff != plan.tariff or c.start < a.start or c.end > a.end or not a.minimum_c <= c.minimum_c <= c.target_c <= c.maximum_c <= a.maximum_c or c.ambient_c != a.ambient_c or c.warming_seconds != a.warming_seconds:
            raise ValueError("冷氣計畫超出核准時段、溫度或模型參數")

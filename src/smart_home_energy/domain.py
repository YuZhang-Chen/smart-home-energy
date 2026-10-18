"""Pure washer, clock, tariff and scheduling rules. All stored dates are UTC."""

from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, time, timedelta
import math
from typing import Any, get_type_hints, get_origin, get_args
from uuid import uuid4
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("時間必須包含時區")
    return value.astimezone(UTC)


def number(value: float, name: str, minimum: float = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} 必須是大於等於 {minimum} 的有限數字")


def encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return aware(value).isoformat()
    if is_dataclass(value):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


def decode(cls: type, data: dict) -> Any:
    """Strict JSON dataclass decoder, including nested snapshots."""
    if not isinstance(data, dict) or set(data) - {f.name for f in fields(cls)}:
        raise ValueError(f"{cls.__name__} 欄位不合法")
    hints = get_type_hints(cls)

    def convert(t: type, v: Any) -> Any:
        origin = get_origin(t)
        args = get_args(t)
        if origin is tuple:
            if not isinstance(v, list):
                raise ValueError("預期陣列")
            return tuple(convert(args[0], i) for i in v)
        if args and type(None) in args:
            return None if v is None else convert(next(a for a in args if a is not type(None)), v)
        if t is datetime:
            if not isinstance(v, str):
                raise ValueError("預期 ISO 時間")
            return aware(datetime.fromisoformat(v))
        if is_dataclass(t):
            return decode(t, v)
        if t in (int, float):
            number(v, "數值", -1e100)
            if t is int and type(v) is not int:
                raise ValueError("預期整數")
        elif t is str and not isinstance(v, str):
            raise ValueError("預期文字")
        elif t is bool and type(v) is not bool:
            raise ValueError("預期布林值")
        return v

    try:
        return cls(**{k: convert(hints[k], v) for k, v in data.items()})
    except (TypeError, KeyError) as error:
        raise ValueError(f"{cls.__name__} 缺少欄位或型別錯誤") from error


@dataclass(frozen=True)
class WashStage:
    name: str
    seconds: float
    power_w: float

    def __post_init__(self) -> None:
        number(self.seconds, "階段秒數", 1)
        number(self.power_w, "階段功率")
        if not self.name:
            raise ValueError("階段名稱不可空白")


@dataclass(frozen=True)
class WashProgram:
    name: str = "標準"
    stages: tuple[WashStage, ...] = (
        WashStage("洗滌", 1800, 200), WashStage("漂洗", 900, 100), WashStage("脫水", 900, 400),
    )

    def __post_init__(self) -> None:
        if not self.name or not self.stages or len(self.stages) > 20 or self.seconds > 86400:
            raise ValueError("行程必須有階段，且不可超過一天")

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.stages)


@dataclass(frozen=True)
class Tariff:
    version: str = "simulated-v1"
    peak_start: int = 8 * 60
    peak_end: int = 22 * 60
    peak: float = 6.0
    offpeak: float = 2.5
    official: dict | None = None

    def __post_init__(self) -> None:
        if not self.version or not 0 <= self.peak_start < self.peak_end <= 1440:
            raise ValueError("尖峰時段不合法")
        number(self.peak, "尖峰費率")
        number(self.offpeak, "離峰費率")

    def rate(self, at: datetime) -> float:
        if self.official is not None:
            from smart_home_energy.tariffs import time_rate
            return time_rate(self.official, at)
        local = aware(at).astimezone(TAIPEI)
        minute = local.hour * 60 + local.minute
        return self.peak if self.peak_start <= minute < self.peak_end else self.offpeak

    def cost(self, start: datetime, seconds: float, power_w: float) -> float:
        if self.official is not None:
            return self.segment_cost([(start, seconds, power_w)])
        number(seconds, "秒數")
        number(power_w, "功率")
        cursor = aware(start)
        end = cursor + timedelta(seconds=seconds)
        total = 0.0
        while cursor < end:
            local = cursor.astimezone(TAIPEI)
            midnight = datetime.combine(local.date(), time(), TAIPEI)
            boundaries = [midnight + timedelta(minutes=m) for m in (self.peak_start, self.peak_end, 1440)]
            boundary = min(b for b in boundaries if b > local).astimezone(UTC)
            stop = min(end, boundary)
            total += power_w * (stop - cursor).total_seconds() / 3_600_000 * self.rate(cursor)
            cursor = stop
        return total

    def segment_cost(self, segments) -> float:
        if self.official is not None:
            from smart_home_energy.tariffs import estimate_incremental_cost
            return estimate_incremental_cost(self.official, segments)
        return sum(self.cost(start, seconds, power) for start, seconds, power in segments)


@dataclass(frozen=True)
class LogicalClock:
    clock_id: str
    real_base: datetime
    logical_base: datetime
    speed: float = 1

    def __post_init__(self) -> None:
        aware(self.real_base)
        aware(self.logical_base)
        number(self.speed, "時間倍率", 1)
        if self.speed not in (1, 60) or not self.clock_id:
            raise ValueError("時間倍率只支援 1 或 60")

    @classmethod
    def create(cls, speed: float = 1, now: datetime | None = None) -> "LogicalClock":
        now = aware(now or utcnow())
        return cls(uuid4().hex, now, now, speed)

    def now(self, real: datetime | None = None) -> datetime:
        return self.logical_base + (aware(real or utcnow()) - self.real_base) * self.speed


@dataclass(frozen=True)
class TaskConstraints:
    earliest: datetime
    deadline: datetime
    quiet_start: int | None = 23 * 60
    quiet_end: int | None = 7 * 60
    device_id: str = "washer-1"

    def __post_init__(self) -> None:
        if aware(self.deadline) <= aware(self.earliest) or self.deadline - self.earliest > timedelta(days=31):
            raise ValueError("期限必須晚於開始，搜尋範圍最多 31 天")
        if self.device_id != "washer-1":
            raise ValueError("只支援 washer-1")
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("安靜時段需同時提供開始與結束")
        if self.quiet_start is not None and (
            type(self.quiet_start) is not int or type(self.quiet_end) is not int
            or not 0 <= self.quiet_start < 1440 or not 0 <= self.quiet_end < 1440
            or self.quiet_start == self.quiet_end
        ):
            raise ValueError("安靜時段需為 0–1439 分鐘且起訖不同；取消請用 null")


@dataclass(frozen=True)
class Plan:
    plan_id: str
    clock_id: str
    start: datetime
    end: datetime
    cost: float
    energy_kwh: float
    program: WashProgram
    tariff: Tariff

    def __post_init__(self) -> None:
        aware(self.start)
        aware(self.end)
        number(self.cost, "費用")
        number(self.energy_kwh, "用電")
        if not self.plan_id or not self.clock_id:
            raise ValueError("計畫與時鐘識別碼不可空白")


@dataclass(frozen=True)
class ApprovalEnvelope:
    constraints: TaskConstraints
    program: WashProgram
    tariff: Tariff
    clock_id: str
    max_cost: float

    def __post_init__(self) -> None:
        number(self.max_cost, "核准費用上限")


def integrate(program: WashProgram, start: datetime, elapsed: float, tariff: Tariff) -> tuple[float, float, str, float]:
    """Integrates the completed part of every stage, even for a very large step."""
    number(elapsed, "經過秒數")
    energy = cost = offset = 0.0
    stage_name, power = "完成", 0.0
    segments = []
    for stage in program.stages:
        used = min(stage.seconds, max(0, elapsed - offset))
        energy += used * stage.power_w / 3_600_000
        segments.append((start + timedelta(seconds=offset), used, stage.power_w))
        if offset <= elapsed < offset + stage.seconds:
            stage_name, power = stage.name, stage.power_w
        offset += stage.seconds
    return energy, tariff.segment_cost(segments), stage_name, power


def quiet_conflict(start: datetime, end: datetime, constraints: TaskConstraints) -> bool:
    if constraints.quiet_start is None:
        return False
    day = start.astimezone(TAIPEI).date() - timedelta(days=1)
    last = end.astimezone(TAIPEI).date()
    while day <= last:
        midnight = datetime.combine(day, time(), TAIPEI)
        lo = midnight + timedelta(minutes=constraints.quiet_start)
        hi = midnight + timedelta(minutes=constraints.quiet_end)
        if hi <= lo:
            hi += timedelta(days=1)
        if start < hi and end > lo:
            return True
        day += timedelta(days=1)
    return False


def make_plan(start: datetime, program: WashProgram, tariff: Tariff, clock_id: str) -> Plan:
    start = aware(start)
    energy, cost, _, _ = integrate(program, start, program.seconds, tariff)
    return Plan(uuid4().hex, clock_id, start, start + timedelta(seconds=program.seconds), cost, energy, program, tariff)


def validate_plan(plan: Plan, constraints: TaskConstraints, now: datetime, clock_id: str,
                  approval: ApprovalEnvelope | None = None) -> None:
    if plan.clock_id != clock_id or plan.start < aware(now):
        raise ValueError("計畫時鐘不符或開始時間已過期，請重新排程")
    calculated = make_plan(plan.start, plan.program, plan.tariff, clock_id)
    if plan.end != calculated.end or not math.isclose(plan.cost, calculated.cost, abs_tol=1e-12) or not math.isclose(plan.energy_kwh, calculated.energy_kwh, abs_tol=1e-12):
        raise ValueError("計畫計算不符")
    if plan.start < constraints.earliest or plan.end > constraints.deadline or quiet_conflict(plan.start, plan.end, constraints):
        raise ValueError("完整行程違反期限或安靜時段")
    if approval is not None:
        if approval.clock_id != clock_id or plan.program != approval.program or plan.tariff != approval.tariff:
            raise ValueError("超出核准的時鐘、行程或費率版本")
        if plan.cost > approval.max_cost + 1e-12:
            raise ValueError("超出核准費用上限")
        validate_plan(plan, approval.constraints, now, clock_id)


def find_schedule(constraints: TaskConstraints, program: WashProgram, tariff: Tariff,
                  now: datetime, clock_id: str, max_cost: float | None = None, prefer_earliest: bool = False) -> Plan:
    earliest = max(aware(now), aware(constraints.earliest))
    start = earliest.replace(second=0, microsecond=0)
    if start < earliest:
        start += timedelta(minutes=1)
    best = None
    while start + timedelta(seconds=program.seconds) <= constraints.deadline:
        if not quiet_conflict(start, start + timedelta(seconds=program.seconds), constraints):
            candidate = make_plan(start, program, tariff, clock_id)
            if (max_cost is None or candidate.cost <= max_cost + 1e-12) and (best is None or candidate.cost < best.cost - 1e-12):
                best = candidate
                if prefer_earliest:
                    return best
        start += timedelta(minutes=1)
    if best is None:
        raise ValueError("沒有合法候選：完整行程、期限、安靜時段或費用上限衝突；請調整限制")
    return best

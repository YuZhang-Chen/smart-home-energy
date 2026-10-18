"""Versioned Taipower residential incremental estimates, never full bills."""
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import json
import math
import re
from urllib.request import urlopen

from smart_home_energy.domain import TAIPEI, Tariff, aware, number, utcnow

SOURCE = "https://service.taipower.com.tw/data/opendata/apply/file/d007008/001.json"
DATA = Path(__file__).with_name("data")
PLANS = {"residential": "表燈非時間電價", "tou2": "簡易型時間電價(二段式)", "tou3": "簡易型時間電價(三段式)"}


def source_snapshot(path=None):
    path = Path(path) if path else DATA / "taipower.json"
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if payload["metadata"]["schema_version"] != "1.0":
        raise ValueError("不支援的台電資料版本")
    for key in PLANS.values():
        if key not in payload["data"]:
            raise ValueError("台電來源缺少已支援的住宅方案")
    return {"raw": payload, "sha256": sha256(raw).hexdigest(), "source_url": SOURCE,
            "retrieved_at": "2026-10-17", "effective_from": payload["metadata"]["實施日期"]}


def configure_tariff(repo, settings):
    plan = settings.get("plan", "simulated")
    if plan not in {"simulated", *PLANS}:
        raise ValueError("不支援的電價方案")
    baseline = settings.get("baseline_kwh", {})
    if not isinstance(baseline, dict):
        raise ValueError("基準用電須為月份與 kWh 對照")
    for month, value in baseline.items():
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("基準月份須為 YYYY-MM")
        number(value, "每月基準用電")
    repo.set_meta("tariff_settings", {"plan": plan, "baseline_kwh": baseline,
        "revision": repo.meta("tariff_settings", {}).get("revision", 0) + 1})


def task_segments(task, repo=None):
    if repo is not None and task.run_id and task.status in {"completed", "aborted", "failed", "resolved"}:
        with repo.transaction() as db:
            for start, end, power in db.execute("SELECT start,end,power_w FROM energy_segments WHERE task_id=? AND run_id=? ORDER BY start", (task.task_id, task.run_id)):
                yield datetime.fromisoformat(start), (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), power
        return
    if task.kind == "washer" and task.plan:
        offset = 0
        for stage in task.plan.program.stages:
            yield task.plan.start + timedelta(seconds=offset), stage.seconds, stage.power_w
            offset += stage.seconds
    elif task.kind == "ac" and task.ac_plan:
        from smart_home_energy.ac_domain import cooling_segments
        p = task.ac_plan
        duration = (p.constraints.end - p.constraints.start).total_seconds()
        yield from cooling_segments(p.constraints, p.initial_c, duration)


def selected_tariff(repo, fallback=None, exclude_task=None):
    config = repo.meta("tariff_settings", {"plan": "simulated"})
    if config["plan"] == "simulated":
        return fallback or Tariff()
    source = repo.meta("tariff_source") or source_snapshot()
    calendar = repo.meta("tariff_calendar") or json.loads((DATA / "calendar.json").read_text(encoding="utf-8"))
    reservations = []
    with repo.transaction() as db:
        from smart_home_energy.repository import Task
        from smart_home_energy.domain import decode
        for row in db.execute("SELECT data FROM tasks ORDER BY rowid"):
            task = decode(Task, json.loads(row[0]))
            if task.task_id == exclude_task or task.status not in {"scheduled", "starting", "running", "stopping", "unknown", "completed", "aborted", "failed", "resolved"}:
                continue
            if not (task.approval or task.ac_approval):
                continue
            reservations += [[start.isoformat(), seconds, power] for start, seconds, power in task_segments(task, repo)]
    reserved = {}
    for text, seconds, power in reservations:
        cursor = datetime.fromisoformat(text)
        end = cursor + timedelta(seconds=seconds)
        while cursor < end:
            local = cursor.astimezone(TAIPEI)
            boundary = (local.replace(day=28, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=4)).replace(day=1)
            stop = min(end, boundary)
            month = local.strftime("%Y-%m")
            reserved[month] = reserved.get(month, 0) + power * (stop - cursor).total_seconds() / 3_600_000
            cursor = stop
    official = {**source, "calendar": calendar, "plan": config["plan"], "baseline_kwh": config["baseline_kwh"],
                "profile_revision": config["revision"], "reserved_kwh": reserved}
    official["raw"] = {"metadata": source["raw"]["metadata"], "data": {PLANS[config["plan"]]: source["raw"]["data"][PLANS[config["plan"]]]}}
    version = "taipower-" + source["sha256"][:12] + "-" + config["plan"]
    return Tariff(version=version, official=official)


def check_date(snapshot, at):
    local = aware(at).astimezone(TAIPEI)
    if local.date().isoformat() < snapshot["effective_from"]:
        raise ValueError("日期早於正式費率實施日期")
    if snapshot["plan"] != "residential" and str(local.year) not in snapshot["calendar"]["years"]:
        raise ValueError("缺少該年度台電離峰日日曆，請更新官方日曆")
    month = local.strftime("%Y-%m")
    if month not in snapshot["baseline_kwh"]:
        raise ValueError(f"請補充 {month} 不含本系統任務的基準用電 (kWh)")
    return local


def time_rate(snapshot, at):
    local = check_date(snapshot, at)
    if snapshot["plan"] == "residential":
        raise ValueError("住宅非時間電價沒有單一時段單價，請使用增量費用估算")
    table = snapshot["raw"]["data"][PLANS[snapshot["plan"]]]["流動電費"]
    season = "夏月" if 6 <= local.month <= 9 else "非夏月"
    offday = local.weekday() >= 5 or local.date().isoformat() in snapshot["calendar"]["offpeak_dates"]
    if offday:
        return table["週六、週日及離峰日"]["離峰時間"][season]["單價"]
    minute = local.hour * 60 + local.minute
    for category in table["週一至週五"].values():
        entry = category.get(season)
        if entry:
            for span in entry["時段"].split("/"):
                lo, hi = span.split("~")
                a, b = [int(x[:2]) * 60 + int(x[3:]) for x in (lo, hi)]
                if a <= minute < b:
                    return entry["單價"]
    raise ValueError("官方時段資料不完整，無法估算")


def split_segments(snapshot, segments):
    for start, seconds, power in segments:
        number(seconds, "區段秒數")
        number(power, "功率")
        cursor, end = aware(start), aware(start) + timedelta(seconds=seconds)
        while cursor < end:
            local = check_date(snapshot, cursor)
            boundary = (local.replace(second=0, microsecond=0) + timedelta(minutes=1)).astimezone(cursor.tzinfo)
            stop = min(end, boundary)
            yield local, power * (stop - cursor).total_seconds() / 3_600_000
            cursor = stop


def tier_charge(snapshot, month, energy):
    summer = 6 <= int(month[5:]) <= 9
    table = snapshot["raw"]["data"]["表燈非時間電價"]["住宅用"]
    levels = [(120, "120度以下部分"), (330, "121~330度部分"), (500, "331~500度部分"),
              (700, "501~700度部分"), (1000, "701~1000度部分"), (math.inf, "1001度以上部分")]
    total, lower = 0.0, 0
    for upper, key in levels:
        used = max(0, min(energy, upper) - lower)
        total += used * table[key]["夏月" if summer else "非夏月"]["單價"]
        lower = upper
    return total


def estimate_incremental_cost(snapshot, segments):
    monthly, time_cost, reserved = {}, 0.0, dict(snapshot.get("reserved_kwh", {}))
    for local, energy in split_segments(snapshot, segments):
        month = local.strftime("%Y-%m")
        monthly[month] = monthly.get(month, 0) + energy
        if snapshot["plan"] != "residential":
            time_cost += energy * time_rate(snapshot, local)
    # Only reservation months relevant to this estimate need user baseline data.
    for start, seconds, power in snapshot.get("reserved_segments", []):
        cursor, end = datetime.fromisoformat(start), datetime.fromisoformat(start) + timedelta(seconds=seconds)
        while cursor < end:
            local = cursor.astimezone(TAIPEI)
            next_month = (local.replace(day=28, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=4)).replace(day=1)
            stop = min(end, next_month)
            month = local.strftime("%Y-%m")
            reserved[month] = reserved.get(month, 0) + power * (stop - cursor).total_seconds() / 3_600_000
            cursor = stop
    for month, energy in monthly.items():
        baseline = snapshot["baseline_kwh"][month] + reserved.get(month, 0)
        if snapshot["plan"] == "residential":
            time_cost += tier_charge(snapshot, month, baseline + energy) - tier_charge(snapshot, month, baseline)
        else:
            text = snapshot["raw"]["data"][PLANS[snapshot["plan"]]]["流動電費"]["每月總度數超過2,000度之部分"]["單價"]
            surcharge = float(str(text).removeprefix("加"))
            time_cost += (max(0, baseline + energy - 2000) - max(0, baseline - 2000)) * surcharge
    return time_cost


def household_estimate(repo, fallback=None):
    """Price the combined approved load once, avoiding independent-tier sums."""
    tariff = selected_tariff(repo, fallback)
    from smart_home_energy.repository import Task
    from smart_home_energy.domain import decode
    with repo.transaction() as db:
        tasks = [decode(Task, json.loads(row[0])) for row in db.execute("SELECT data FROM tasks ORDER BY rowid")]
    usage = [segment for task in tasks if task.status != "cancelled" and (task.approval or task.ac_approval) for segment in task_segments(task, repo)]
    if not tariff.official:
        return tariff.segment_cost(usage)
    snapshot = {**tariff.official, "reserved_segments": [], "reserved_kwh": {}}
    segments = []
    for start, seconds, power in usage:
        end = start + timedelta(seconds=seconds)
        while start < end:
            local = start.astimezone(TAIPEI)
            next_month = (local.replace(day=28, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=4)).replace(day=1)
            stop = min(end, next_month)
            if local.strftime("%Y-%m") in snapshot["baseline_kwh"]:
                segments.append((start, (stop - start).total_seconds(), power))
            start = stop
    return estimate_incremental_cost(snapshot, segments)


def refresh_source(repo):
    """Explicit user operation; retain last good source on any download error."""
    with urlopen(SOURCE, timeout=20) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("官方資料超過上限")
    payload = json.loads(raw.decode("utf-8-sig"))
    if payload["metadata"]["schema_version"] != "1.0" or not all(k in payload["data"] for k in PLANS.values()):
        raise ValueError("官方資料格式已改變，保留既有版本")
    snapshot = {"raw": payload, "sha256": sha256(raw).hexdigest(), "source_url": SOURCE,
                "retrieved_at": utcnow().isoformat(), "effective_from": payload["metadata"]["實施日期"]}
    repo.set_meta("tariff_source", snapshot)
    return snapshot["effective_from"]

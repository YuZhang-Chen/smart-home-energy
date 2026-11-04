"""Offline scenario runner and descriptive reports; never controls appliances."""
import argparse
import csv
from dataclasses import replace
from datetime import datetime, timedelta, UTC
from hashlib import sha256
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import subprocess
from time import perf_counter_ns
from uuid import uuid4

from .lighting_rules import (LightingRule, EnvironmentClock, Environment, Presence,
                             LightingState, evaluate, manual_override, resume, switch_clock)

SCENARIO_VERSION = "1"
BASE = datetime(2026, 11, 2, 10, tzinfo=UTC)  # 18:00 Taipei


def scenario(name):
    rule = LightingRule(enabled=True)
    clock = EnvironmentClock("offline", BASE, BASE)
    env = Environment(clock, BASE)
    state = LightingState()
    if name in ("day", "day_boundary", "midnight", "night", "night_boundary", "disabled"):
        hour = {"day": 4, "day_boundary": 22, "midnight": 16}.get(name, 10)
        env = replace(env, real_now=BASE.replace(hour=hour))
        rule = replace(rule, enabled=name != "disabled")
        result = evaluate(rule, env, state)
        expected = None if name == "disabled" else name not in ("day", "day_boundary")
        assert (result.target.power if result.target else None) == expected
    elif name == "override_resume":
        state = manual_override(rule, env, state)
        assert evaluate(rule, env, state).target is None
        assert evaluate(rule, env, resume(state)).target.power
        assert evaluate(rule, replace(env, real_now=BASE + timedelta(hours=12)), state).target.power is False
    elif name in ("absence", "presence_expired", "present", "unknown"):
        rule = replace(rule, presence_enabled=True)
        value = {"present": "present", "unknown": "unknown"}.get(name, "absent")
        env = replace(env, presence=Presence.observe(value, BASE))
        result = evaluate(rule, env, state)
        if name == "absence":
            assert result.target is None
            result = evaluate(rule, replace(env, real_now=BASE + timedelta(minutes=5)), result.state)
            assert result.target.power is False
        elif name == "presence_expired":
            assert evaluate(rule, replace(env, real_now=BASE + timedelta(minutes=15)), result.state).target is None
        elif name == "present":
            assert result.target.power
        else:
            assert result.target is None
    elif name == "acceleration":
        clock = replace(clock, speed=60)
        assert clock.now(BASE + timedelta(seconds=5)) == BASE + timedelta(minutes=5)
    elif name == "roundtrip_stability":
        first = evaluate(rule, env, state)
        restored = LightingState.from_dict(first.state.to_dict())
        assert evaluate(rule, env, restored).decision_id == first.decision_id
    elif name == "custom_window":
        rule = replace(rule, day_start=1200, night_start=240)
        result = evaluate(rule, env, state)
        assert result.window == "night" and result.target.power
    elif name == "clock_generation":
        new_clock, clean = switch_clock(LightingRule(), BASE, BASE, 60, "new", "offline")
        old = LightingState("offline", BASE, BASE + timedelta(days=1))
        result = evaluate(rule, Environment(new_clock, BASE), old)
        assert clean.absent_since is None and result.state.override_until is None
    elif name == "absence_refresh":
        rule = replace(rule, presence_enabled=True)
        first = evaluate(rule, replace(env, presence=Presence.observe("absent", BASE)), state)
        second = evaluate(rule, replace(env, real_now=BASE + timedelta(minutes=4), presence=Presence.observe("absent", BASE + timedelta(minutes=4))), first.state)
        assert second.target is None and second.state.absent_since == BASE
        third = evaluate(rule, replace(env, real_now=BASE + timedelta(minutes=5), presence=Presence.observe("absent", BASE + timedelta(minutes=5))), second.state)
        assert third.target.power is False
        fourth = evaluate(rule, replace(env, real_now=BASE + timedelta(minutes=6), presence=Presence.observe("present", BASE + timedelta(minutes=6))), third.state)
        assert fourth.target.power and fourth.state.absent_since is None
    elif name == "expected_rejection":
        try:
            LightingRule(brightness=float("nan"))
        except ValueError:
            return
        raise AssertionError("invalid brightness accepted")
    else:
        raise ValueError(name)


SCENARIOS = ("disabled", "day", "day_boundary", "night", "night_boundary", "midnight",
             "override_resume", "absence", "presence_expired", "present", "unknown",
             "acceleration", "roundtrip_stability", "expected_rejection", "custom_window",
             "clock_generation", "absence_refresh")
EXCLUSIONS = [
    {"scenario": "CHAT-01", "status": "known_issue", "reason": "SSE frame cursors deferred"},
    {"scenario": "CHAT-02", "status": "known_issue", "reason": "Conversation restart recovery deferred"},
    {"scenario": "device_arbitration", "status": "not_implemented", "reason": "Device integration prerequisite"},
    {"scenario": "mqtt_matter", "status": "not_executed", "reason": "Offline rules only"},
]


def percentile(values, fraction):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(rows):
    result = []
    for name in sorted({r["scenario"] for r in rows}):
        samples = [r for r in rows if r["scenario"] == name]
        executed = [r for r in samples if r["status"] in ("passed", "failed")]
        durations = [r["duration_ms"] for r in executed if r["duration_ms"] is not None]
        good = sum(r["status"] == "passed" for r in executed)
        result.append(dict(scenario=name, samples=len(executed), passed=good, failed=len(executed)-good,
                           success_rate=good/len(executed) if executed else None,
                           p50_ms=percentile(durations, .5), p95_ms=percentile(durations, .95),
                           excluded=len(samples)-len(executed)))
    return result


def metadata():
    root = Path(__file__).resolve().parents[2]
    def git(*args):
        try:
            return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
    revision = git("rev-parse", "HEAD")
    diff = git("diff", "HEAD", "--binary")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    digest = None
    if diff is not None and untracked is not None:
        hasher = sha256(diff)
        for filename in sorted(untracked.split(b"\0")):
            if filename:
                try:
                    content = (root / filename.decode("utf-8")).read_bytes()
                except (OSError, UnicodeError):
                    digest = "unavailable"
                    break
                hasher.update(len(filename).to_bytes(8, "big") + filename + sha256(content).digest())
        else:
            digest = hasher.hexdigest()
    return dict(revision=revision.decode().strip() if revision else "unavailable",
                workspace_hash=digest or "unavailable", python=platform.python_version(),
                dependencies={d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata["Name"]},
                transport="not_used", model="not_used", scenario_version=SCENARIO_VERSION,
                clock_modes=[1, 60])


def run(output, repeats=10, scenarios=SCENARIOS, execute=scenario):
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be positive")
    directory = Path(output) / ("offline-" + uuid4().hex)
    directory.mkdir(parents=True)
    rows = []
    for name in scenarios:
        for repetition in range(1, repeats+1):
            start = perf_counter_ns()
            status, error = "passed", None
            try:
                execute(name)
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
            rows.append(dict(scenario=name, repetition=repetition, status=status, reason=error,
                             duration_ms=(perf_counter_ns()-start)/1e6, device_confirmation_ms=None,
                             chat_recovery_ms=None, model_requests=None))
    rows.extend(dict(item, repetition=None, duration_ms=None, device_confirmation_ms=None,
                     chat_recovery_ms=None, model_requests=None) for item in EXCLUSIONS)
    summary = summarize(rows)
    passed = all(r["status"] != "failed" for r in rows)
    document = dict(metadata=metadata(), repeats=repeats, scope_status="passed" if passed else "failed",
                    overall_acceptance="incomplete", results=rows, summary=summary)
    (directory / "results.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    with (directory / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = list(summary[0]) if summary else ["scenario", "samples", "passed", "failed", "success_rate", "p50_ms", "p95_ms", "excluded"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)
    lines = ["# Offline lighting report", f"Scope: {document['scope_status']}; overall acceptance: incomplete.",
             "Durations measure complete offline rule scenarios, not model or appliance latency.",
             "| Scenario | Samples | Passed | Failed | P50 ms | P95 ms | Excluded |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in summary:
        name = row["scenario"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {row['samples']} | {row['passed']} | {row['failed']} | {row['p50_ms']} | {row['p95_ms']} | {row['excluded']} |")
    lines.extend(f"\n- {item['scenario']}: {item['status']} — {item['reason']}" for item in EXCLUSIONS)
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory, passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--output", default="artifacts/research")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    directory, passed = run(args.output, args.repeats)
    print(directory.resolve())
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

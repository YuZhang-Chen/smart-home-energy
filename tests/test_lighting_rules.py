from dataclasses import replace
from datetime import datetime, timedelta, UTC
import pytest

from smart_home_energy.lighting_rules import *
from smart_home_energy.research import SCENARIOS, scenario

BASE = datetime(2026, 11, 2, 10, tzinfo=UTC)


@pytest.mark.parametrize("name", SCENARIOS)
def test_fixed_scenarios(name):
    scenario(name)


@pytest.mark.parametrize("changes", [dict(day_start=360, night_start=360), dict(day_start=-1),
    dict(night_start=1440), dict(day_start=True), dict(brightness=float("nan")),
    dict(kelvin=float("inf")), dict(brightness=0), dict(kelvin=2600), dict(enabled=1)])
def test_reject_invalid_rule(changes):
    with pytest.raises(ValueError):
        LightingRule(**changes)


def test_naive_time_rejected():
    with pytest.raises(ValueError):
        EnvironmentClock("a", datetime(2026, 1, 1), BASE)
    with pytest.raises(ValueError):
        Presence.observe("absent", datetime(2026, 1, 1))


def test_custom_wrapping_day_and_conversions():
    rule = LightingRule(enabled=True, day_start=1200, night_start=240, brightness=40, kelvin=6500)
    clock = EnvironmentClock("a", BASE, BASE)
    result = evaluate(rule, Environment(clock, BASE), LightingState())
    assert result.window == "night"
    assert result.target == LightingTarget(True, 102, 154)
    assert result.next_boundary.astimezone(TAIPEI).hour == 20


def test_presence_countdown_refresh_and_expiry_gap():
    rule = LightingRule(enabled=True, presence_enabled=True)
    clock = EnvironmentClock("a", BASE, BASE)
    p = Presence.observe("absent", BASE)
    first = evaluate(rule, Environment(clock, BASE, p), LightingState())
    before = evaluate(rule, Environment(clock, BASE + timedelta(minutes=4, seconds=59), p), first.state)
    assert before.target is None
    p2 = Presence.observe("absent", BASE + timedelta(minutes=4))
    end = evaluate(rule, Environment(clock, BASE + timedelta(minutes=5), p2), before.state)
    assert end.target == LightingTarget(False)
    assert end.state.absent_since == BASE
    expired = evaluate(rule, Environment(clock, BASE + timedelta(minutes=19), p2), end.state)
    assert expired.target is None and expired.state.absent_since is None
    fresh = Presence.observe("absent", BASE + timedelta(minutes=20))
    restored = evaluate(rule, Environment(clock, BASE + timedelta(minutes=20), fresh), end.state)
    assert restored.target is None  # Gap must not inherit the earlier countdown.
    present = Presence.observe("present", BASE + timedelta(minutes=21))
    result = evaluate(rule, Environment(clock, BASE + timedelta(minutes=21), present), restored.state)
    assert result.target.power and result.state.absent_since is None


def test_accelerated_absence_and_real_ttl_roundtrip():
    rule = LightingRule(enabled=True, presence_enabled=True)
    clock = EnvironmentClock("a", BASE, BASE, 60)
    p = Presence.observe("absent", BASE)
    first = evaluate(rule, Environment(clock, BASE, p), LightingState())
    state = LightingState.from_dict(first.state.to_dict())
    assert evaluate(rule, Environment(clock, BASE + timedelta(seconds=5), p), state).target.power is False
    assert evaluate(rule, Environment(clock, BASE + timedelta(seconds=899), p), state).reason != "presence_unknown"
    assert evaluate(rule, Environment(clock, BASE + timedelta(seconds=900), p), state).reason == "presence_unknown"


def test_switch_requires_disabled_new_generation_and_clears_timers():
    with pytest.raises(ValueError):
        switch_clock(LightingRule(enabled=True), BASE, BASE, 60, "b", "a")
    with pytest.raises(ValueError):
        switch_clock(LightingRule(), BASE, BASE, 60, "a", "a")
    clock, state = switch_clock(LightingRule(), BASE, BASE, 60, "b", "a")
    assert state == LightingState("b") and clock.speed == 60
    old = LightingState("a", BASE, BASE + timedelta(days=1))
    result = evaluate(LightingRule(enabled=True), Environment(clock, BASE), old)
    assert result.target.power and result.state.override_until is None


def test_unknown_future_presence_and_override_expiry():
    rule = LightingRule(enabled=True, presence_enabled=True)
    clock = EnvironmentClock("a", BASE, BASE)
    future = Presence.observe("present", BASE + timedelta(seconds=1))
    assert evaluate(rule, Environment(clock, BASE, future), LightingState()).target is None
    env = Environment(clock, BASE, Presence.observe("present", BASE))
    state = manual_override(rule, env, LightingState())
    assert evaluate(rule, env, state).reason == "manual_override"
    assert evaluate(rule, env, resume(state)).target.power

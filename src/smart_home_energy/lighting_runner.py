"""Background runner bridging pure lighting_rules into SQLite requests and BulbController."""

from datetime import datetime, timedelta
from hashlib import sha256
import json
import logging
import sqlite3

from smart_home_energy.domain import aware, decode, encode, utcnow
from smart_home_energy.lighting_rules import (
    Environment,
    EnvironmentClock,
    LightingRule,
    LightingState,
    boundaries,
    evaluate,
    manual_override,
    resume,
)

LOGGER = logging.getLogger(__name__)

RULE_KEY = "lighting_rule"
STATE_KEY = "lighting_state"
CURSOR_KEY = "lighting_manual_cursor"
RULE_SOURCE = "lighting_rule"
MAX_ATTEMPTS = 3
RETRY_INTERVAL = timedelta(seconds=10)


def load_rule(repo) -> LightingRule:
    raw = repo.meta(RULE_KEY)
    if not raw:
        return LightingRule()
    return decode(LightingRule, raw)


def _default_runner_state() -> dict:
    return {
        "inner": LightingState().to_dict(),
        "epoch": 0,
        "last_decision_id": None,
        "attempt": 0,
        "last_request_id": None,
        "last_dispatch_at": None,
        "last_reason": "disabled",
        "last_window": None,
        "next_boundary": None,
        "evaluated_at": None,
        "last_error": None,
    }


def load_runner_state(repo) -> dict:
    raw = repo.meta(STATE_KEY)
    base = _default_runner_state()
    if isinstance(raw, dict):
        base.update(raw)
    return base


def _env_clock_from_repo(repo) -> EnvironmentClock:
    clock = repo.clock()
    speed = 60 if int(round(clock.speed)) == 60 else 1
    return EnvironmentClock(clock.clock_id, clock.real_base, clock.logical_base, speed)


def lighting_snapshot(repo, real: datetime | None = None) -> dict:
    """Return full lighting rule and current evaluation status for API/UI/Agent."""
    real = aware(real or utcnow())
    rule = load_rule(repo)
    state = load_runner_state(repo)
    inner = LightingState.from_dict(state["inner"]) if state.get("inner") else LightingState()
    clock = _env_clock_from_repo(repo)
    now = clock.now(real)
    window, _start, following = boundaries(rule, now)

    override_until = inner.override_until.isoformat() if inner.override_until and now < inner.override_until else None
    last_req_id = state.get("last_request_id")
    last_req = repo.request(last_req_id) if last_req_id else None

    if not rule.enabled:
        reason = "disabled"
    elif override_until:
        reason = "manual_override"
    else:
        reason = state.get("last_reason") or window

    return {
        "rule": {
            "enabled": rule.enabled,
            "day_start": rule.day_start,
            "night_start": rule.night_start,
            "brightness": rule.brightness,
            "kelvin": rule.kelvin,
            "presence_enabled": False,
            "version": rule.version,
        },
        "status": {
            "window": state.get("last_window") or window,
            "reason": reason,
            "override_until": override_until,
            "next_boundary": state.get("next_boundary") or following.isoformat(),
            "last_request_id": last_req_id,
            "last_request_status": last_req["status"] if last_req else None,
            "last_error": state.get("last_error"),
            "evaluated_at": state.get("evaluated_at"),
        },
    }


def lighting_summary(repo, real: datetime | None = None) -> dict:
    """Compact summary for /api/home and Light Agent context."""
    snap = lighting_snapshot(repo, real)
    rule = snap["rule"]
    status = snap["status"]
    return {
        "enabled": rule["enabled"],
        "day_start": rule["day_start"],
        "night_start": rule["night_start"],
        "brightness": rule["brightness"],
        "kelvin": rule["kelvin"],
        "version": rule["version"],
        "window": status["window"],
        "reason": status["reason"],
        "override_until": status["override_until"],
        "next_boundary": status["next_boundary"],
        "last_error": status["last_error"],
    }


class LightingRuleRunner:
    """Evaluates lighting rules on each controller tick and submits set_bulb requests."""

    def __init__(self, repo):
        self.repo = repo

    def environment(self, real: datetime) -> Environment:
        return Environment(_env_clock_from_repo(self.repo), aware(real), presence=None)

    def _process_settings(self, real: datetime) -> None:
        with self.repo.transaction() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT rowid, id, kind, payload FROM requests "
                    "WHERE status='pending' AND kind IN ('lighting_rule', 'lighting_resume') "
                    "ORDER BY rowid"
                )
            ]
        for row in rows:
            try:
                with self.repo.transaction():
                    payload = json.loads(row["payload"])
                    current_rule = load_rule(self.repo)
                    state = load_runner_state(self.repo)
                    inner = LightingState.from_dict(state["inner"])

                    if row["kind"] == "lighting_rule":
                        if payload.get("presence_enabled") is True:
                            raise ValueError("人體感應功能尚未啟用")
                        expected = payload.get("expected_version")
                        if expected != current_rule.version:
                            raise ValueError("自動照明規則版本已改變，請刷新後重試")
                        new_rule = LightingRule(
                            enabled=payload["enabled"],
                            day_start=payload["day_start"],
                            night_start=payload["night_start"],
                            brightness=payload["brightness"],
                            kelvin=payload["kelvin"],
                            presence_enabled=False,
                            version=current_rule.version + 1,
                        )
                        self.repo.set_meta(RULE_KEY, encode(new_rule))
                        if not current_rule.enabled and new_rule.enabled:
                            state["epoch"] = int(state.get("epoch", 0)) + 1
                            state["inner"] = resume(inner).to_dict()
                            state["last_decision_id"] = None
                            state["last_request_id"] = None
                            state["last_dispatch_at"] = None
                            state["attempt"] = 0
                            state["last_error"] = None
                            self.repo.set_meta(CURSOR_KEY, int(row["rowid"]))
                        elif current_rule.enabled and not new_rule.enabled:
                            state["last_decision_id"] = None
                            state["last_request_id"] = None
                            state["last_dispatch_at"] = None
                            state["attempt"] = 0
                            state["last_error"] = None
                        else:
                            state["last_error"] = None
                        self.repo.set_meta(STATE_KEY, state)
                        self.repo.audit("lighting_rule_updated", encode(new_rule))
                        self.repo.finish_request(row["id"], "done", "自動照明規則已更新")

                    elif row["kind"] == "lighting_resume":
                        state["inner"] = resume(inner).to_dict()
                        state["epoch"] = int(state.get("epoch", 0)) + 1
                        state["last_decision_id"] = None
                        state["last_request_id"] = None
                        state["last_dispatch_at"] = None
                        state["attempt"] = 0
                        state["last_error"] = None
                        self.repo.set_meta(STATE_KEY, state)
                        self.repo.audit(
                            "lighting_rule_resumed",
                            {"epoch": state["epoch"], "rule_version": current_rule.version},
                        )
                        self.repo.finish_request(row["id"], "done", "已恢復自動照明")
            except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as error:
                self.repo.finish_request(row["id"], "error", str(error))

    def _detect_manual(self, rule: LightingRule, env: Environment, state: dict) -> LightingState:
        inner = LightingState.from_dict(state["inner"])
        cursor = int(self.repo.meta(CURSOR_KEY, 0) or 0)
        with self.repo.transaction() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT rowid, id, payload FROM requests "
                    "WHERE kind='set_bulb' AND rowid > ? ORDER BY rowid",
                    (cursor,),
                )
            ]
        if not rows:
            return inner
        new_cursor = max(int(r["rowid"]) for r in rows)
        manual_rows = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("source") != RULE_SOURCE:
                manual_rows.append(r)

        self.repo.set_meta(CURSOR_KEY, new_cursor)
        if rule.enabled and manual_rows:
            inner = manual_override(rule, env, inner)
            state["inner"] = inner.to_dict()
            state["last_error"] = None
            self.repo.audit(
                "lighting_manual_override",
                {
                    "trigger_request_id": manual_rows[-1]["id"],
                    "override_until": inner.override_until.isoformat() if inner.override_until else None,
                },
            )
        return inner

    def _dispatch(self, rule: LightingRule, ev, state: dict, real: datetime) -> None:
        epoch = int(state.get("epoch", 0))
        attempt = int(state.get("attempt", 0))
        raw_key = f"{ev.decision_id}:{epoch}:{attempt}"
        request_id = "rule-" + sha256(raw_key.encode()).hexdigest()[:24]

        if ev.target.power is False:
            payload = {
                "device_id": "bulb-1",
                "power": False,
                "source": RULE_SOURCE,
                "authorization_ref": f"rule:{rule.version}",
            }
        else:
            payload = {
                "device_id": "bulb-1",
                "power": True,
                "brightness_percent": rule.brightness,
                "kelvin": rule.kelvin,
                "duration_seconds": 2.0,
                "source": RULE_SOURCE,
                "authorization_ref": f"rule:{rule.version}",
            }

        self.repo.submit(request_id, "set_bulb", payload)
        state["last_request_id"] = request_id
        state["last_dispatch_at"] = real.isoformat()
        self.repo.audit(
            "lighting_rule_dispatched",
            {
                "request_id": request_id,
                "decision_id": ev.decision_id,
                "reason": ev.reason,
                "attempt": attempt,
                "rule_version": rule.version,
            },
        )

    def _evaluate_and_dispatch(self, real: datetime) -> None:
        with self.repo.transaction():
            rule = load_rule(self.repo)
            state = load_runner_state(self.repo)
            env = self.environment(real)
            inner = self._detect_manual(rule, env, state)

            ev = evaluate(rule, env, inner)
            state["inner"] = ev.state.to_dict()
            state["last_reason"] = ev.reason
            state["last_window"] = ev.window
            state["next_boundary"] = ev.next_boundary.isoformat()
            state["evaluated_at"] = real.isoformat()

            if ev.target is None or ev.decision_id is None:
                if ev.reason == "disabled":
                    state["last_error"] = None
                self.repo.set_meta(STATE_KEY, state)
                return

            if ev.decision_id != state.get("last_decision_id"):
                state["last_decision_id"] = ev.decision_id
                state["attempt"] = 0
                state["last_request_id"] = None
                state["last_dispatch_at"] = None
                state["last_error"] = None

            last_req_id = state.get("last_request_id")
            if last_req_id is None:
                self._dispatch(rule, ev, state, real)
            else:
                req = self.repo.request(last_req_id)
                action = self.repo.bulb_action(last_req_id)
                if req is None:
                    self._dispatch(rule, ev, state, real)
                elif req["status"] == "done":
                    state["last_error"] = None
                elif req["status"] == "error":
                    if action and action.get("status") == "superseded":
                        pass
                    else:
                        detail = req.get("result") or "設備未確認"
                        attempt = int(state.get("attempt", 0))
                        if attempt + 1 < MAX_ATTEMPTS:
                            last_at = state.get("last_dispatch_at")
                            due = last_at is None or (real - aware(datetime.fromisoformat(last_at))) >= RETRY_INTERVAL
                            state["last_error"] = f"自動照明暫未套用，將重試：{detail}"
                            if due:
                                state["attempt"] = attempt + 1
                                self._dispatch(rule, ev, state, real)
                        else:
                            state["last_error"] = f"自動照明未能套用（已重試 {MAX_ATTEMPTS} 次）：{detail}"

            self.repo.set_meta(STATE_KEY, state)

    def tick(self, real: datetime | None = None) -> None:
        real = aware(real or utcnow())
        try:
            self._process_settings(real)
            self._evaluate_and_dispatch(real)
        except Exception as error:  # pragma: no cover - defensive isolation in controller loop
            LOGGER.exception("LightingRuleRunner tick 失敗")
            try:
                self.repo.audit("lighting_rule_error", {"error": str(error)})
            except Exception:
                pass

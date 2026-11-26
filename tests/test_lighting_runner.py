"""Tests for LightingRuleRunner integration with SQLite, BulbController, and VirtualBulb."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_home_energy.bulb import VirtualBulb
from smart_home_energy.bulb_control import BulbController
from smart_home_energy.domain import LogicalClock, TAIPEI
from smart_home_energy.lighting_runner import LightingRuleRunner, RULE_SOURCE, lighting_snapshot
from smart_home_energy.repository import Repository
from test_control import FakeTransport


def _taipei(year, month, day, hour, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=TAIPEI).astimezone(timezone.utc)


class LightingRuleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(self.directory / "control.db")
        self.bulb_repo = Repository(self.directory / "bulb.db")
        self.transport = FakeTransport()
        self.runner = LightingRuleRunner(self.repo)
        self.bulb_ctrl = BulbController(self.repo, self.transport)

    def _set_clock(self, now: datetime, speed: int = 1, clock_id: str = "clk-1") -> None:
        self.repo.set_meta("clock", LogicalClock(clock_id, now, now, speed))

    def _init_bulb(self, now: datetime) -> VirtualBulb:
        bulb = VirtualBulb(self.bulb_repo, now)
        self.repo.save_device(bulb.state)
        return bulb

    def _enable_rule(
        self,
        now: datetime,
        *,
        expected_version: int = 1,
        enabled: bool = True,
        day_start: int = 360,
        night_start: int = 1080,
        brightness: float = 40,
        kelvin: float = 2700,
        request_id: str = "rule-cfg-1",
    ) -> None:
        self.repo.submit(
            request_id,
            "lighting_rule",
            {
                "device_id": "bulb-1",
                "expected_version": expected_version,
                "enabled": enabled,
                "day_start": day_start,
                "night_start": night_start,
                "brightness": brightness,
                "kelvin": kelvin,
                "presence_enabled": False,
            },
        )
        self.runner.tick(now)

    def _set_bulb_requests(self) -> list[dict]:
        with self.repo.transaction() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM requests WHERE kind='set_bulb' ORDER BY rowid")
            ]

    def _drive_bulb(self, bulb: VirtualBulb, now: datetime, duration: float = 2.0) -> None:
        self.bulb_ctrl.tick(now)
        if not self.transport.sent:
            return
        cmd = self.transport.sent[-1][1]
        res = bulb.handle(cmd, now)
        self.transport.inbox.append(("result", res))
        self.transport.inbox.append(("state", bulb.tick(now + timedelta(seconds=duration))))
        self.bulb_ctrl.tick(now + timedelta(seconds=duration))

    def test_disabled_rule_dispatches_nothing(self):
        now = _taipei(2026, 11, 26, 21, 0)
        self._set_clock(now)
        self._init_bulb(now)
        self.runner.tick(now)
        self.assertEqual(self._set_bulb_requests(), [])
        snap = lighting_snapshot(self.repo, now)
        self.assertFalse(snap["rule"]["enabled"])
        self.assertEqual(snap["status"]["reason"], "disabled")

    def test_day_dispatches_once_and_does_not_repeat(self):
        now = _taipei(2026, 11, 26, 12, 0)
        self._set_clock(now)
        bulb = self._init_bulb(now)
        self._enable_rule(now)
        reqs = self._set_bulb_requests()
        self.assertEqual(len(reqs), 1)
        payload = json.loads(reqs[0]["payload"])
        self.assertFalse(payload["power"])
        self.assertEqual(payload["source"], RULE_SOURCE)

        self._drive_bulb(bulb, now, duration=0.5)
        self.assertEqual(self.repo.request(reqs[0]["id"])["status"], "done")

        # Subsequent ticks in the same day window must not dispatch again.
        self.runner.tick(now + timedelta(seconds=5))
        self.runner.tick(now + timedelta(minutes=30))
        self.assertEqual(len(self._set_bulb_requests()), 1)

    def test_night_dispatches_40_percent_warm_and_confirms(self):
        now = _taipei(2026, 11, 26, 21, 0)
        self._set_clock(now)
        bulb = self._init_bulb(now)
        self._enable_rule(now)
        reqs = self._set_bulb_requests()
        self.assertEqual(len(reqs), 1)
        payload = json.loads(reqs[0]["payload"])
        self.assertTrue(payload["power"])
        self.assertEqual(payload["brightness_percent"], 40)
        self.assertEqual(payload["kelvin"], 2700)

        self._drive_bulb(bulb, now, duration=2.0)
        self.assertEqual(self.repo.request(reqs[0]["id"])["status"], "done")
        self.assertTrue(bulb.state.power)
        self.assertEqual(bulb.state.level, 102)
        self.assertEqual(bulb.state.mired, 370)

    def test_midnight_and_boundary_transitions(self):
        t_day = _taipei(2026, 11, 26, 17, 59)
        self._set_clock(t_day)
        bulb = self._init_bulb(t_day)
        self._enable_rule(t_day)
        self._drive_bulb(bulb, t_day, duration=0.5)
        self.assertEqual(len(self._set_bulb_requests()), 1)

        # Cross 18:00 -> night
        t_night = _taipei(2026, 11, 26, 18, 0)
        self.repo.save_device(bulb.tick(t_night))
        self.runner.tick(t_night)
        self.assertEqual(len(self._set_bulb_requests()), 2)
        self._drive_bulb(bulb, t_night, duration=2.0)

        # Cross midnight (23:59 -> 00:01): still same night window, no duplicate dispatch
        t_late = _taipei(2026, 11, 26, 23, 59)
        self.runner.tick(t_late)
        t_after_midnight = _taipei(2026, 11, 27, 0, 1)
        self.runner.tick(t_after_midnight)
        self.assertEqual(len(self._set_bulb_requests()), 2)

        # Cross 06:00 -> day
        t_next_day = _taipei(2026, 11, 27, 6, 0)
        self.repo.save_device(bulb.tick(t_next_day))
        self.runner.tick(t_next_day)
        self.assertEqual(len(self._set_bulb_requests()), 3)

    def test_manual_override_pauses_until_next_boundary_and_resume_redispatches(self):
        now = _taipei(2026, 11, 26, 21, 0)
        self._set_clock(now)
        bulb = self._init_bulb(now)
        self._enable_rule(now)
        self._drive_bulb(bulb, now, duration=2.0)
        self.assertEqual(len(self._set_bulb_requests()), 1)

        # Manual turn-off at 21:05
        t_manual = now + timedelta(minutes=5)
        self.repo.save_device(bulb.tick(t_manual))
        self.repo.submit("manual-off", "set_bulb", {"device_id": "bulb-1", "power": False})
        self.runner.tick(t_manual)
        self._drive_bulb(bulb, t_manual, duration=0.5)
        self.assertFalse(bulb.state.power)

        snap = lighting_snapshot(self.repo, t_manual)
        self.assertEqual(snap["status"]["reason"], "manual_override")
        self.assertIsNotNone(snap["status"]["override_until"])

        # Still paused at 22:00
        self.runner.tick(_taipei(2026, 11, 26, 22, 0))
        self.assertEqual(len(self._set_bulb_requests()), 2)  # 1 rule + 1 manual

        # Explicit resume at 22:05 immediately re-dispatches night target (epoch bumped)
        t_resume = _taipei(2026, 11, 26, 22, 5)
        self.repo.save_device(bulb.tick(t_resume))
        self.repo.submit("resume-1", "lighting_resume", {"device_id": "bulb-1"})
        self.runner.tick(t_resume)
        self.assertEqual(self.repo.request("resume-1")["status"], "done")
        self.assertEqual(len(self._set_bulb_requests()), 3)
        self._drive_bulb(bulb, t_resume, duration=2.0)
        self.assertTrue(bulb.state.power)

        # Another manual override expires naturally at 06:00 next day
        t_manual2 = _taipei(2026, 11, 26, 22, 10)
        self.repo.submit("manual-off-2", "set_bulb", {"device_id": "bulb-1", "power": False})
        self.runner.tick(t_manual2)
        self.assertEqual(lighting_snapshot(self.repo, t_manual2)["status"]["reason"], "manual_override")

        t_morning = _taipei(2026, 11, 27, 6, 0)
        self.repo.save_device(bulb.tick(t_morning))
        self.runner.tick(t_morning)
        snap_morning = lighting_snapshot(self.repo, t_morning)
        self.assertEqual(snap_morning["status"]["reason"], "day")
        self.assertIsNone(snap_morning["status"]["override_until"])
        self.assertEqual(len(self._set_bulb_requests()), 5)

    def test_manual_request_supersedes_pending_rule_request(self):
        now = _taipei(2026, 11, 26, 21, 0)
        self._set_clock(now)
        bulb = self._init_bulb(now)
        self._enable_rule(now)
        rule_req_id = self._set_bulb_requests()[0]["id"]

        # Manual request submitted before BulbController drains the rule request
        self.repo.submit("manual-wins", "set_bulb", {"device_id": "bulb-1", "power": False, "duration_seconds": 0})
        self.bulb_ctrl.tick(now)
        self.assertEqual(self.repo.bulb_action(rule_req_id)["status"], "superseded")
        self.assertEqual(len(self.transport.sent), 1)
        self.assertFalse(self.transport.sent[0][1].power)

        # Next runner tick detects manual override and does not retry the superseded rule request
        self.runner.tick(now + timedelta(seconds=1))
        self.assertEqual(lighting_snapshot(self.repo, now + timedelta(seconds=1))["status"]["reason"], "manual_override")
        self.assertEqual(len(self._set_bulb_requests()), 2)

    def test_stale_bulb_retries_up_to_three_times_then_reports_error(self):
        now = _taipei(2026, 11, 26, 21, 0)
        self._set_clock(now)
        # No bulb state saved -> BulbController rejects with stale error
        self._enable_rule(now)
        self.bulb_ctrl.tick(now)
        self.assertEqual(len(self._set_bulb_requests()), 1)
        self.assertEqual(self._set_bulb_requests()[0]["status"], "error")

        # 5s later: before 10s retry interval, should not retry yet
        self.runner.tick(now + timedelta(seconds=5))
        self.assertEqual(len(self._set_bulb_requests()), 1)

        # 11s later: attempt 2
        t2 = now + timedelta(seconds=11)
        self.runner.tick(t2)
        self.bulb_ctrl.tick(t2)
        self.assertEqual(len(self._set_bulb_requests()), 2)

        # 22s later: attempt 3
        t3 = t2 + timedelta(seconds=11)
        self.runner.tick(t3)
        self.bulb_ctrl.tick(t3)
        self.assertEqual(len(self._set_bulb_requests()), 3)

        # 33s later: retries exhausted -> no 4th attempt, error recorded
        t4 = t3 + timedelta(seconds=11)
        self.runner.tick(t4)
        self.assertEqual(len(self._set_bulb_requests()), 3)
        snap = lighting_snapshot(self.repo, t4)
        self.assertIn("已重試 3 次", snap["status"]["last_error"])

    def test_clock_switch_clears_override_and_reevaluates(self):
        now = _taipei(2026, 11, 26, 21, 0)
        self._set_clock(now, speed=1, clock_id="clk-a")
        bulb = self._init_bulb(now)
        self._enable_rule(now)
        self._drive_bulb(bulb, now, duration=2.0)

        t1 = now + timedelta(seconds=5)
        self.repo.submit("manual-1", "set_bulb", {"device_id": "bulb-1", "power": False})
        self.runner.tick(t1)
        self.assertEqual(lighting_snapshot(self.repo, t1)["status"]["reason"], "manual_override")

        # Switch to 60x clock with new generation
        t2 = t1 + timedelta(seconds=5)
        self._set_clock(t2, speed=60, clock_id="clk-b")
        self.runner.tick(t2)
        snap = lighting_snapshot(self.repo, t2)
        self.assertEqual(snap["status"]["reason"], "night")
        self.assertIsNone(snap["status"]["override_until"])

    def test_restart_restores_state_without_duplicate_dispatch(self):
        now = _taipei(2026, 11, 26, 21, 0)
        self._set_clock(now)
        bulb = self._init_bulb(now)
        self._enable_rule(now)
        self._drive_bulb(bulb, now, duration=2.0)
        self.assertEqual(len(self._set_bulb_requests()), 1)

        # Simulate controller restart with a new Repository & LightingRuleRunner instance
        reopened = Repository(self.repo.path)
        restarted_runner = LightingRuleRunner(reopened)
        restarted_runner.tick(now + timedelta(seconds=30))
        with reopened.transaction() as db:
            count = db.execute("SELECT COUNT(*) FROM requests WHERE kind='set_bulb'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_rejects_version_mismatch_and_presence_enabled(self):
        now = _taipei(2026, 11, 26, 12, 0)
        self._set_clock(now)
        self._enable_rule(now, expected_version=99, request_id="bad-ver")
        self.assertEqual(self.repo.request("bad-ver")["status"], "error")
        self.assertIn("版本", self.repo.request("bad-ver")["result"])

        self.repo.submit(
            "bad-presence",
            "lighting_rule",
            {
                "device_id": "bulb-1",
                "expected_version": 1,
                "enabled": True,
                "day_start": 360,
                "night_start": 1080,
                "brightness": 40,
                "kelvin": 2700,
                "presence_enabled": True,
            },
        )
        self.runner.tick(now)
        self.assertEqual(self.repo.request("bad-presence")["status"], "error")
        self.assertIn("人體感應", self.repo.request("bad-presence")["result"])


if __name__ == "__main__":
    unittest.main()

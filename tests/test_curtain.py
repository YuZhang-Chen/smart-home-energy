"""Tests for CurtainController, durable operation logs, 60x logical-clock schedules, and Curtain Agent schedule management."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from smart_home_energy import curtain
from smart_home_energy.api import create_app
from smart_home_energy.config import HemsSettings
from smart_home_energy.curtain_control import CurtainController
from smart_home_energy.domain import TAIPEI, LogicalClock, encode
from smart_home_energy.multi_agent import run_household
from smart_home_energy.repository import Repository
from test_agent import FakeModel, action


class CurtainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.repo = Repository(self.directory / "hems.db")
        self.hems = replace(HemsSettings.from_env(), database=str(self.directory / "hems.db"))
        self.app = create_app(self.repo, self.hems, self.directory / "empty-dist")
        self.client = TestClient(self.app)
        self.controller = CurtainController(self.repo)
        self._orig_data_file = curtain.DATA_FILE
        self._orig_state_file = curtain.STATE_FILE
        curtain.DATA_FILE = self.directory / "curtain_schedules.json"
        curtain.STATE_FILE = self.directory / "curtain_state.json"
        curtain.schedules.clear()
        curtain.move_to(0.0, duration=0.0)

    def tearDown(self):
        curtain.DATA_FILE = self._orig_data_file
        curtain.STATE_FILE = self._orig_state_file
        self.temp.cleanup()

    def test_durable_requests_controller_and_operation_logs(self):
        real = datetime(2026, 11, 28, 1, 0, 0, tzinfo=TAIPEI)
        # 1. Submit curtain_position via /api/requests
        r1 = self.client.post(
            "/api/requests",
            json={
                "request_id": "curtain-pos-1",
                "kind": "curtain_position",
                "payload": {"device_id": "curtain-1", "position": 60, "duration_seconds": 0},
            },
        )
        self.assertEqual(r1.status_code, 202)
        self.controller.tick(real)
        req1 = self.client.get("/api/requests/curtain-pos-1").json()
        self.assertEqual(req1["status"], "done")
        self.assertEqual(self.client.get("/api/curtain/status").json()["position"], 60.0)

        # 2. Submit scene + schedule create / toggle / delete via /api/requests
        self.assertEqual(
            self.client.post(
                "/api/requests",
                json={
                    "request_id": "curtain-sched-create",
                    "kind": "curtain_schedule_create",
                    "payload": {"device_id": "curtain-1", "time": "07:30", "position": 100, "enabled": True},
                },
            ).status_code,
            202,
        )
        self.controller.tick(real + timedelta(seconds=1))
        schedules = self.client.get("/api/curtain/schedules").json()
        self.assertEqual(len(schedules), 1)
        sched_id = schedules[0]["id"]

        self.assertEqual(
            self.client.post(
                "/api/requests",
                json={
                    "request_id": "curtain-sched-toggle",
                    "kind": "curtain_schedule_toggle",
                    "payload": {"device_id": "curtain-1", "schedule_id": sched_id, "enabled": False},
                },
            ).status_code,
            202,
        )
        self.controller.tick(real + timedelta(seconds=2))
        self.assertFalse(self.client.get("/api/curtain/schedules").json()[0]["enabled"])

        self.assertEqual(
            self.client.post(
                "/api/requests",
                json={
                    "request_id": "curtain-sched-delete",
                    "kind": "curtain_schedule_delete",
                    "payload": {"device_id": "curtain-1", "schedule_id": sched_id},
                },
            ).status_code,
            202,
        )
        self.controller.tick(real + timedelta(seconds=3))
        self.assertEqual(self.client.get("/api/curtain/schedules").json(), [])

        # 3. Verify durable operation logs in /api/diagnostics and devices table
        diag = self.client.get("/api/diagnostics").json()
        audit_kinds = {row["kind"] for row in diag["audit"]}
        self.assertTrue(
            {
                "curtain_position_set",
                "curtain_schedule_created",
                "curtain_schedule_toggled",
                "curtain_schedule_deleted",
            }
            <= audit_kinds
        )
        curtain_cmds = [json.loads(r["data"]) for r in diag["commands"] if json.loads(r["data"]).get("device_id") == "curtain-1"]
        self.assertGreaterEqual(len(curtain_cmds), 1)
        self.assertIsNotNone(self.repo.device("curtain-1"))
        self.assertEqual(self.repo.device("curtain-1")["position"], 60.0)

    def test_60x_demo_clock_triggers_curtain_schedule_once_per_logical_day(self):
        # Establish a deterministic 60x LogicalClock starting at 06:58:00 Taipei
        base = datetime(2026, 11, 28, 6, 58, 0, tzinfo=TAIPEI)
        clock = LogicalClock(clock_id="demo-60x", real_base=base, logical_base=base, speed=60.0)
        self.repo.set_meta("clock", encode(clock))

        # Schedule 07:00 -> 100% open
        curtain.create_schedule_entry("07:00", 100.0, enabled=True, repo=self.repo, source="test", real=base)
        self.assertEqual(curtain.get_status(repo=self.repo)["position"], 0.0)

        # Tick 1 at base (06:58 logical) -> establishes baseline, does not trigger
        self.controller.tick(base)
        self.assertEqual(curtain.get_status(repo=self.repo)["target"], 0.0)

        # Tick 2 at base + 1 real second -> 06:59 logical, still before 07:00
        self.controller.tick(base + timedelta(seconds=1))
        self.assertEqual(curtain.get_status(repo=self.repo)["target"], 0.0)

        # Tick 3 at base + 2.5 real seconds -> 07:00:30 logical (crossed 07:00 under 60x speed!)
        self.controller.tick(base + timedelta(seconds=2.5))
        self.assertEqual(curtain.get_status(repo=self.repo)["target"], 100.0)

        # Verify audit log recorded curtain_schedule_triggered with 60x speed
        audits = [row for row in self.repo.records("audit", 50) if row["kind"] == "curtain_schedule_triggered"]
        self.assertEqual(len(audits), 1)
        payload = json.loads(audits[0]["data"])
        self.assertEqual(payload["time"], "07:00")
        self.assertEqual(payload["clock_speed"], 60.0)

        # Subsequent tick on same logical day must NOT trigger again
        curtain.move_to(20.0, duration=0.0, repo=self.repo, source="test", real=base + timedelta(seconds=3))
        self.controller.tick(base + timedelta(seconds=4))
        self.assertEqual(curtain.get_status(repo=self.repo)["target"], 20.0)
        audits_after = [row for row in self.repo.records("audit", 50) if row["kind"] == "curtain_schedule_triggered"]
        self.assertEqual(len(audits_after), 1)

        # Switching clock_id resets baseline without misfiring
        clock2 = LogicalClock(clock_id="demo-1x", real_base=base + timedelta(seconds=10), logical_base=base + timedelta(hours=25), speed=1.0)
        self.repo.set_meta("clock", encode(clock2))
        self.controller.tick(base + timedelta(seconds=10))
        resets = [row for row in self.repo.records("audit", 50) if row["kind"] == "curtain_scheduler_clock_reset"]
        self.assertEqual(len(resets), 1)

    def test_main_agent_delegates_curtain_schedule_create_disable_and_delete_with_llm(self):
        # 1. Create schedule via Main Agent -> Curtain Agent
        self.repo.event("household:c-sched-1", None, {"root_id": "c-sched-1", "text": "每天早上 07:00 打開窗簾"})
        replies_create = [
            action("get_home_state"),
            action("delegate_tasks", requests=[{"device_id": "curtain-1", "text": "每天早上 07:00 打開窗簾"}]),
            action("schedule_curtain", device_id="curtain-1", time="07:00", position=100, enabled=True),
        ]
        run1 = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies_create)]))
        self.assertEqual(run1.status, "completed")
        scheds = curtain.list_schedules_data(self.repo)
        self.assertEqual(len(scheds), 1)
        self.assertTrue(scheds[0]["enabled"])

        # 2. Disable schedule via Main Agent -> Curtain Agent
        self.repo.event("household:c-sched-2", None, {"root_id": "c-sched-2", "text": "停用 07:00 的窗簾排程"})
        replies_disable = [
            action("get_home_state"),
            action("delegate_tasks", requests=[{"device_id": "curtain-1", "text": "停用 07:00 的窗簾排程"}]),
            action("set_curtain_schedule_enabled", device_id="curtain-1", time="07:00", enabled=False),
        ]
        run2 = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies_disable)]))
        self.assertEqual(run2.status, "completed")
        self.assertIn("停用", run2.message)
        self.assertFalse(curtain.list_schedules_data(self.repo)[0]["enabled"])

        # 3. Delete schedule via Main Agent -> Curtain Agent
        self.repo.event("household:c-sched-3", None, {"root_id": "c-sched-3", "text": "刪除 07:00 的窗簾排程"})
        replies_delete = [
            action("get_home_state"),
            action("delegate_tasks", requests=[{"device_id": "curtain-1", "text": "刪除 07:00 的窗簾排程"}]),
            action("delete_curtain_schedule", device_id="curtain-1", time="07:00"),
        ]
        run3 = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies_delete)]))
        self.assertEqual(run3.status, "completed")
        self.assertIn("刪除", run3.message)
        self.assertEqual(curtain.list_schedules_data(self.repo), [])

        # Verify MCP manifest exposes new tools
        manifest = self.client.get("/api/mcp/manifest").json()
        curtain_tool_names = {t["name"] for t in manifest["tools_by_role"]["Curtain Agent"]}
        self.assertTrue(
            {"get_curtain_context", "set_curtain_position", "stop_curtain_movement", "schedule_curtain", "delete_curtain_schedule", "set_curtain_schedule_enabled"}
            <= curtain_tool_names
        )

    def test_deterministic_curtain_agent_supports_schedule_disable_enable_and_delete(self):
        # Create
        self.repo.event("household:det-1", None, {"root_id": "det-1", "text": "設定窗簾每天 08:30 半開排程"})
        r1 = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, providers=[]))
        self.assertEqual(r1.status, "completed")
        self.assertEqual(len(curtain.list_schedules_data(self.repo)), 1)

        # Disable
        self.repo.event("household:det-2", None, {"root_id": "det-2", "text": "停用 08:30 窗簾排程"})
        r2 = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, providers=[]))
        self.assertEqual(r2.status, "completed")
        self.assertFalse(curtain.list_schedules_data(self.repo)[0]["enabled"])

        # Enable
        self.repo.event("household:det-3", None, {"root_id": "det-3", "text": "啟用 08:30 窗簾排程"})
        r3 = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, providers=[]))
        self.assertEqual(r3.status, "completed")
        self.assertTrue(curtain.list_schedules_data(self.repo)[0]["enabled"])

        # Delete
        self.repo.event("household:det-4", None, {"root_id": "det-4", "text": "刪除 08:30 窗簾排程"})
        r4 = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, providers=[]))
        self.assertEqual(r4.status, "completed")
        self.assertEqual(curtain.list_schedules_data(self.repo), [])

    def test_mixed_curtain_and_light_request_is_not_hijacked_by_light_only_shortcut(self):
        from smart_home_energy.light_agent import is_explicit_light_only_request

        text = "裡面太暗了開一點窗簾，順便把客廳燈打開調到 60%"
        self.assertFalse(is_explicit_light_only_request(text))
        self.assertFalse(is_explicit_light_only_request("打開電視並把客廳燈調到 50%"))
        self.assertTrue(is_explicit_light_only_request("把客廳燈打開調到 60%"))


if __name__ == "__main__":
    unittest.main()

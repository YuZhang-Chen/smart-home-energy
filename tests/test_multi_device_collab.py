"""Stress tests for quirky, multi-device simultaneous collaboration via Main Agent."""

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

from smart_home_energy import curtain, tv
from smart_home_energy.ac import VirtualAC
from smart_home_energy.bulb import VirtualBulb
from smart_home_energy.bulb_control import BulbController
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import TAIPEI, utcnow
from smart_home_energy.light_agent import is_explicit_light_only_request
from smart_home_energy.multi_agent import (
    explicit_ac_request,
    explicit_ac_stop_request,
    is_explicit_curtain_only_request,
    is_explicit_tv_only_request,
    run_household,
)
from smart_home_energy.repository import Repository
from smart_home_energy.washer import Washer
from test_agent import FakeModel, action
from test_control import FakeTransport


class MultiDeviceCollaborationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.repo = Repository(self.directory / "hems.db")
        self.hems = replace(HemsSettings.from_env(), database=str(self.directory / "hems.db"), agent_requests=15)
        self.real = utcnow()
        self._orig_curtain_data = curtain.DATA_FILE
        self._orig_curtain_state = curtain.STATE_FILE
        self._orig_tv_data = tv.DATA_FILE
        curtain.DATA_FILE = self.directory / "curtain_schedules.json"
        curtain.STATE_FILE = self.directory / "curtain_state.json"
        tv.DATA_FILE = self.directory / "tv_state.json"
        curtain.schedules.clear()
        curtain.move_to(50.0, duration=0.0)
        tv.update_tv(power=True, volume=50, muted=False, input_source="HDMI1", playback="stopped")

        # Seed fresh virtual telemetry for bulb-1, ac-1, washer-1
        clock = self.repo.clock()
        self.bulb = VirtualBulb(Repository(self.directory / "bulb.db"), self.real)
        self.repo.save_device(self.bulb.state)
        self.ac = VirtualAC(Repository(self.directory / "ac.db"), clock, self.real)
        self.repo.save_device(self.ac.state)
        self.washer = Washer(Repository(self.directory / "washer.db"), clock, self.real)
        self.repo.save_device(self.washer.state)

    def tearDown(self):
        curtain.DATA_FILE = self._orig_curtain_data
        curtain.STATE_FILE = self._orig_curtain_state
        tv.DATA_FILE = self._orig_tv_data
        self.temp.cleanup()

    def test_shortcut_routers_never_hijack_mixed_device_requests(self):
        mixed_prompts = [
            "裡面太暗了開一點窗簾，順便把客廳燈打開調到 60%",
            "把電視關掉，再開客廳燈到 80%",
            "把冷氣關掉順便把電視音量調低",
            "開冷氣 26 度並切到 HDMI 2",
            "拉上窗簾順便把洗衣機排程在今晚洗",
            "窗簾開 30%、客廳燈 70%、電視靜音、冷氣 25 度",
            "關冷氣並停用 07:00 窗簾排程",
            "開電視看電影，同時把燈調暖 2700K 並拉上窗簾",
        ]
        for text in mixed_prompts:
            with self.subTest(text=text):
                self.assertFalse(is_explicit_light_only_request(text))
                self.assertIsNone(explicit_ac_request(text))
                self.assertFalse(explicit_ac_stop_request(text))
                self.assertFalse(is_explicit_tv_only_request(text))
                self.assertFalse(is_explicit_curtain_only_request(text))

    def test_cross_clause_opposite_verbs_turn_off_tv_and_curtain_but_turn_on_light(self):
        """Ensure '把電視關掉，把窗簾全關，再把客廳燈打開調到 80%' does not misread '關掉...燈'."""
        prompt = "把電視關掉、把窗簾全關，再把客廳燈打開調到 80% 4500K"
        self.repo.event("household:opposite-verbs", None, {"root_id": "opposite-verbs", "text": prompt})
        replies = [
            action("get_home_state"),
            action(
                "delegate_tasks",
                requests=[
                    {"device_id": "tv-1", "text": "把電視關掉"},
                    {"device_id": "curtain-1", "text": "把窗簾全關"},
                    {"device_id": "bulb-1", "text": "把客廳燈打開調到 80% 4500K"},
                ],
            ),
            action("control_tv", device_id="tv-1", power=False),
            action("set_curtain_position", device_id="curtain-1", position=0, duration_seconds=0),
            action("submit_light_action", device_id="bulb-1", power=True, brightness_percent=80, kelvin=4500, duration_seconds=0.5),
        ]
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies)]))
        self.assertEqual(run.status, "completed")
        self.assertFalse(tv.get_status()["power"])
        self.assertEqual(curtain.get_status(repo=self.repo)["position"], 0.0)

        # Confirm bulb controller dispatches and confirms 80% 4500K
        transport = FakeTransport()
        ctrl = BulbController(self.repo, transport)
        ctrl.tick(self.real)
        cmd = transport.sent[0][1]
        done_at = self.real + timedelta(seconds=1)
        transport.inbox.extend([("result", self.bulb.handle(cmd, self.real)), ("state", self.bulb.tick(done_at))])
        ctrl.tick(done_at)
        self.assertTrue(self.repo.device("bulb-1")["power"])

    def test_multi_percentage_numbers_stay_isolated_across_curtain_bulb_and_tv(self):
        """Three different numbers in one sentence: curtain 35%, bulb 75%, TV volume 45."""
        prompt = "窗簾開到 35%，客廳燈亮度調到 75%，電視音量調到 45"
        self.repo.event("household:multi-pct", None, {"root_id": "multi-pct", "text": prompt})
        replies = [
            action("get_home_state"),
            action(
                "delegate_tasks",
                requests=[
                    {"device_id": "curtain-1", "text": "窗簾開到 35%"},
                    {"device_id": "bulb-1", "text": "客廳燈亮度調到 75%"},
                    {"device_id": "tv-1", "text": "電視音量調到 45"},
                ],
            ),
            action("set_curtain_position", device_id="curtain-1", position=35, duration_seconds=0),
            action("submit_light_action", device_id="bulb-1", power=True, brightness_percent=75, duration_seconds=0.5),
            action("control_tv", device_id="tv-1", volume=45),
        ]
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies)]))
        self.assertEqual(run.status, "completed")
        self.assertEqual(curtain.get_status(repo=self.repo)["position"], 35.0)
        self.assertEqual(tv.get_status()["volume"], 45)
        self.assertIn("bulb-1", run.message)

    def test_all_five_devices_simultaneous_horror_movie_night_orchestration(self):
        """Ultimate 5-device orchestration: curtain + tv + bulb + ac + washer in one Main Agent delegation."""
        prompt = (
            "晚上要看恐怖片，拉上窗簾全關、打開電視切到 HDMI3 音量 35 開始播放、"
            "客廳燈調暗到 15% 暖光 2700K、冷氣開 24 度吹兩小時、洗衣機順便用便宜時段洗衣服"
        )
        self.repo.event("household:five-way", None, {"root_id": "five-way", "text": prompt})

        # Compute valid AC and washer plans dynamically through a stateful FakeModel
        class FiveDeviceModel:
            name = "fake-five"

            def __init__(self):
                self.step = 0
                self.ac_plan = None
                self.ac_ver = 1
                self.washer_bundle = None
                self.washer_ver = 1

            async def complete(self, messages, _timeout):
                return json.dumps(self._next_action(messages), ensure_ascii=False)

            def _next_action(self, messages):
                self.step += 1
                last_user = messages[-1]["content"]
                if last_user.startswith("{") and '"error"' in last_user:
                    raise AssertionError(f"Agent received error at step {self.step}: {last_user}")
                sys_prompt = messages[0]["content"]
                if "Main Agent" in sys_prompt:
                    if self.step == 1:
                        return action("get_home_state")
                    return action(
                        "delegate_tasks",
                        requests=[
                            {"device_id": "curtain-1", "text": "拉上窗簾全關"},
                            {"device_id": "tv-1", "text": "打開電視切到 HDMI3 音量 35 開始播放"},
                            {"device_id": "bulb-1", "text": "客廳燈調暗到 15% 暖光 2700K"},
                            {"device_id": "ac-1", "text": "冷氣開 24 度吹兩小時"},
                            {"device_id": "washer-1", "text": "洗衣機用便宜時段洗衣服"},
                        ],
                    )
                if "Curtain Agent" in sys_prompt:
                    return action("set_curtain_position", device_id="curtain-1", position=0, duration_seconds=0)
                if "TV Agent" in sys_prompt:
                    return action("control_tv", device_id="tv-1", power=True, input="HDMI3", volume=35, playback="playing")
                if "Light Agent" in sys_prompt:
                    return action("submit_light_action", device_id="bulb-1", power=True, brightness_percent=15, kelvin=2700, duration_seconds=0.5)
                if "AC Agent" in sys_prompt:
                    parsed = json.loads(last_user)
                    if "error" in parsed:
                        raise AssertionError(f"AC error: {parsed}")
                    if "tool_result" not in parsed:
                        self.ac_ver = parsed["task"]["version"]
                        return action("get_device_context")
                    tr = parsed["tool_result"]
                    if "fresh" in tr:
                        return action("prepare_default_plan", duration_minutes=120, target_c=24)
                    if "plan_id" in tr:
                        self.ac_plan = tr
                        return action("validate_plan", plan=tr)
                    return action("propose_plan", plan=self.ac_plan, expected_version=self.ac_ver)
                if "Washer Agent" in sys_prompt:
                    parsed = json.loads(last_user)
                    if "error" in parsed:
                        raise AssertionError(f"Washer error: {parsed}")
                    if "tool_result" not in parsed:
                        self.washer_ver = parsed["task"]["version"]
                        return action("find_default_schedule", immediate=False)
                    tr = parsed["tool_result"]
                    if "constraints" in tr and "plan" in tr:
                        self.washer_bundle = tr
                        return action("propose_plan", plan=tr["plan"], constraints=tr["constraints"], expected_version=self.washer_ver, reason="離峰便宜時段")
                raise AssertionError(f"Unexpected prompt: {sys_prompt[:40]}")

        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FiveDeviceModel()]))
        self.assertEqual(run.status, "completed", run.message)

        # Verify all 5 devices took effect
        self.assertEqual(curtain.get_status(repo=self.repo)["position"], 0.0)
        tv_st = tv.get_status()
        self.assertTrue(tv_st["power"])
        self.assertEqual(tv_st["input"], "HDMI3")
        self.assertEqual(tv_st["volume"], 35)
        self.assertEqual(tv_st["playback"], "playing")
        self.assertIsNotNone(self.repo.active_task("ac-1"))
        self.assertEqual(self.repo.active_task("ac-1").status, "pending")
        self.assertIsNotNone(self.repo.active_task("washer-1"))
        self.assertEqual(self.repo.active_task("washer-1").status, "pending")

        # Verify 1 Main Agent parent -> 5 distinct child device agents in agent_runs
        runs = [json.loads(r["data"]) for r in self.repo.records("agent_runs", 20)]
        children = [r for r in runs if r.get("parent_id") == run.run_id]
        self.assertEqual(
            {c["role"] for c in children},
            {"Curtain Agent", "TV Agent", "Light Agent", "AC Agent", "Washer Agent"},
        )
        self.assertEqual(
            {c["device_id"] for c in children},
            {"curtain-1", "tv-1", "bulb-1", "ac-1", "washer-1"},
        )

    def test_partial_success_when_bulb_is_stale_while_curtain_and_tv_succeed(self):
        """If bulb-1 telemetry is stale, curtain-1 and tv-1 must still succeed and log durable audit entries."""
        stale_bulb = replace(self.bulb.state, observed_at=self.real - timedelta(minutes=5))
        self.repo.save_device(stale_bulb)
        curtain.create_schedule_entry("07:00", 100.0, enabled=True, repo=self.repo, source="test")

        prompt = "停用 07:00 的窗簾排程，電視靜音，並把客廳燈調到 50%"
        self.repo.event("household:partial-1", None, {"root_id": "partial-1", "text": prompt})
        replies = [
            action("get_home_state"),
            action(
                "delegate_tasks",
                requests=[
                    {"device_id": "curtain-1", "text": "停用 07:00 的窗簾排程"},
                    {"device_id": "tv-1", "text": "電視靜音"},
                    {"device_id": "bulb-1", "text": "把客廳燈調到 50%"},
                ],
            ),
            action("set_curtain_schedule_enabled", device_id="curtain-1", time="07:00", enabled=False),
            action("control_tv", device_id="tv-1", muted=True),
        ]
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies)]))
        self.assertEqual(run.status, "partial_success")
        self.assertFalse(curtain.list_schedules_data(self.repo)[0]["enabled"])
        self.assertTrue(tv.get_status()["muted"])
        audits = [row["kind"] for row in self.repo.records("audit", 30)]
        self.assertIn("curtain_schedule_toggled", audits)


if __name__ == "__main__":
    unittest.main()

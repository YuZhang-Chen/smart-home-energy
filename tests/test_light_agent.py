"""Model-independent tests of Light Agent authority and household delegation."""

import asyncio
import os
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_home_energy.agent_roles import agent_roles
from smart_home_energy.bulb import VirtualBulb
from smart_home_energy.bulb_control import BulbController
from smart_home_energy.config import HemsSettings
from smart_home_energy.light_agent import LightTools
from smart_home_energy.multi_agent import run_household
from smart_home_energy.repository import Repository
from smart_home_energy.domain import utcnow
from test_agent import FakeModel, action
from test_control import FakeTransport


class LightAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(self.directory / "control.db")
        self.hems = replace(HemsSettings.from_env(), database=self.repo.path,
                            device_database=str(self.directory / "washer.db"))
        self.bulb = VirtualBulb(Repository(self.directory / "bulb.db"), utcnow())
        self.repo.save_device(self.bulb.state)

    def test_roles_are_declared_and_light_tools_reject_other_devices(self):
        roles = agent_roles()
        self.assertEqual(roles["Light Agent"]["devices"], ["bulb-1"])
        self.assertNotIn("control_device", roles["Main Agent"]["can"])
        tools = LightTools(self.repo, "root", "客廳燈調到 40%")
        with self.assertRaises(ValueError):
            tools.call("submit_light_action", {"device_id": "ac-1", "power": True})
        tools.call("get_light_context", {})
        with self.assertRaises(ValueError):
            tools.call("submit_light_action", {"brightness_percent": 60})
        vague = LightTools(self.repo, "vague", "調整客廳燈")
        vague.call("get_light_context", {})
        with self.assertRaises(ValueError):
            vague.call("submit_light_action", {"power": True})
        warm = LightTools(self.repo, "warm", "把客廳燈改成暖光")
        warm.call("get_light_context", {})
        with self.assertRaises(ValueError):
            warm.call("submit_light_action", {"kelvin": 4000})
        unrelated = LightTools(self.repo, "other", "幫我開冷氣")
        unrelated.call("get_light_context", {})
        with self.assertRaises(ValueError):
            unrelated.call("submit_light_action", {"power": True})
        with self.assertRaises(ValueError):
            tools.call("create_rule", {"at": "21:00"})
        self.assertEqual(self.repo.pending_requests(), [])

    def test_language_delegates_and_queues_one_light_action_without_claiming_completion(self):
        self.repo.event("household:light", None,
                        {"root_id": "light-root", "text": "幫我把客廳燈調到 40%，暖色 2700K"})
        replies = [action("get_home_state"), action("get_agent_roles"),
                   action("delegate_tasks", requests=[{"device_id": "bulb-1", "text": "客廳燈 40%，2700K"}]),
                   action("get_light_context"),
                   action("submit_light_action", device_id="bulb-1", power=True,
                          brightness_percent=40, kelvin=2700)]
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies)]))
        self.assertEqual(run.status, "completed")
        requests = self.repo.pending_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["kind"], "set_bulb")
        self.assertEqual(self.repo.device("bulb-1")["power"], False)
        self.assertIn("等待設備回報", run.message)
        self.assertEqual(self.repo.records("delegations")[0]["device_id"], "bulb-1")

    def test_bulb_delegation_ignores_scheduler_fields_copied_by_model(self):
        self.repo.event("household:light-copy", None,
                        {"root_id": "light-copy", "text": "關燈"})
        event = self.repo.claim_event()
        replies = [action("get_home_state"),
                   action("delegate_tasks", requests=[{"device_id": "bulb-1", "text": "關燈",
                                                       "task_id": "stale-task", "expected_version": 7}]),
                   action("get_light_context"),
                   action("submit_light_action", device_id="bulb-1", power=False)]
        run = asyncio.run(run_household(self.repo, event, self.hems, [FakeModel(replies)]))
        self.assertEqual(run.status, "completed")
        self.assertEqual(self.repo.pending_requests()[0]["kind"], "set_bulb")

    def test_language_to_confirmed_virtual_bulb(self):
        self.repo.event("household:full", None, {"root_id": "full", "text": "客廳燈調到 40% 暖光 2700K"})
        replies = [action("get_home_state"),
                   action("delegate_tasks", requests=[{"device_id": "bulb-1", "text": "客廳燈 40% 暖光 2700K"}]),
                   action("get_light_context"),
                   action("submit_light_action", device_id="bulb-1", power=True, brightness_percent=40, kelvin=2700, duration_seconds=0)]
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies)]))
        self.assertEqual(run.status, "completed")
        request = self.repo.pending_requests()[0]
        transport = FakeTransport()
        control = BulbController(self.repo, transport)
        now = utcnow()
        control.tick(now)
        command = transport.sent[0][1]
        result = self.bulb.handle(command, now)
        later = now + timedelta(seconds=1)
        state = self.bulb.tick(later)
        transport.inbox.extend([("result", result), ("state", state)])
        control.tick(later)
        self.assertEqual(self.repo.bulb_action(request["id"])["status"], "confirmed")
        self.assertTrue(self.repo.device("bulb-1")["power"])
        self.assertEqual(self.repo.device("bulb-1")["level"], 102)

    def test_explicit_30_percent_light_request_works_without_model_and_confirms_device_feedback(self):
        text = "請將客廳智慧燈泡的亮度調整為 30%，完成後查詢設備狀態，確認是否真的調整成功。"
        self.repo.event("household:light-30", None, {"root_id": "light-30", "text": text})
        event = self.repo.claim_event()

        # No configured model is supplied: an unambiguous absolute target must
        # still use the authorised Light Agent path and avoid malformed LLM JSON.
        run = asyncio.run(run_household(self.repo, event, self.hems, providers=[]))
        self.assertEqual(run.status, "completed", run.message)
        self.assertIn("等待燈泡設備回報確認", run.message)

        request = next(item for item in self.repo.pending_requests() if item["kind"] == "set_bulb")
        import json
        payload = json.loads(request["payload"])
        self.assertEqual(payload["brightness_percent"], 30)
        self.assertIsNone(payload["power"])

        transport = FakeTransport()
        controller = BulbController(self.repo, transport)
        now = utcnow()
        controller.tick(now)
        self.assertEqual(len(transport.sent), 1)
        command = transport.sent[0][1]
        result = self.bulb.handle(command, now)
        later = now + timedelta(seconds=1)
        state = self.bulb.tick(later)
        transport.inbox.extend([("result", result), ("state", state)])
        controller.tick(later)

        action = self.repo.bulb_action(request["id"])
        self.assertEqual(action["status"], "confirmed")
        self.assertEqual(self.repo.device("bulb-1")["level"], 76)
        self.assertEqual(self.repo.request(request["id"])["status"], "done")

    @unittest.skipUnless(os.getenv("RUN_LIGHT_LIVE_TEST") == "1", "Set RUN_LIGHT_LIVE_TEST=1 to use the configured model")
    def test_real_model_delegates_light_and_device_confirms(self):
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(1):
                state = self.bulb.tick(utcnow())
                self.repo.save_device(state)
        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            self.repo.event("household:live-light", None, {"root_id": "live-light", "text": "請把客廳燈調到 40% 暖光 2700K"})
            run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems))
            self.assertIn(run.status, {"completed", "partial_success"}, run.message)
            requests = [item for item in self.repo.pending_requests() if item["kind"] == "set_bulb"]
            self.assertEqual(len(requests), 1, run.message)
            transport = FakeTransport()
            control = BulbController(self.repo, transport)
            now = utcnow()
            control.tick(now)
            command = transport.sent[0][1]
            transport.inbox.extend([("result", self.bulb.handle(command, now)), ("state", self.bulb.tick(now))])
            control.tick(now)
            if self.bulb.state.transitioning:
                from datetime import timedelta
                later = now + timedelta(seconds=command.duration_seconds)
                transport.inbox.append(("state", self.bulb.tick(later)))
                control.tick(later)
            self.assertEqual(self.repo.bulb_action(requests[0]["id"])["status"], "confirmed")
            self.assertEqual(self.repo.device("bulb-1")["level"], 102)
        finally:
            stop.set()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

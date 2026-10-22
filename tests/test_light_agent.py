"""Model-independent tests of Light Agent authority and household delegation."""

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_home_energy.agent_roles import agent_roles
from smart_home_energy.bulb import VirtualBulb
from smart_home_energy.config import HemsSettings
from smart_home_energy.light_agent import LightTools
from smart_home_energy.multi_agent import run_household
from smart_home_energy.repository import Repository
from smart_home_energy.domain import utcnow
from test_agent import FakeModel, action


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


if __name__ == "__main__":
    unittest.main()

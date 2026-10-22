from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from smart_home_energy.bulb import VirtualBulb
from smart_home_energy.bulb_control import BulbController
from smart_home_energy.bulb_domain import BulbCommand, kelvin_to_mired, level_to_percent, percent_to_level
from smart_home_energy.repository import Repository
from test_control import FakeTransport


class BulbTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(self.directory / "bulb.db")
        self.real = datetime(2026, 10, 20, 13, tzinfo=timezone.utc)
        self.bulb = VirtualBulb(self.repo, self.real)

    def command(self, identifier="set-1", **changes):
        fields = dict(command_id=identifier, action_id=identifier, action="set",
                      expected_boot=self.bulb.state.boot_id,
                      expected_state_version=self.bulb.state.state_version,
                      generation=self.bulb.state.generation + 1,
                      expires_at=self.real + timedelta(seconds=10),
                      authorization_ref="request:human", duration_seconds=0)
        fields.update(changes)
        return BulbCommand(**fields)

    def test_startup_is_off_and_40_percent_warm_light_is_persistent(self):
        self.assertFalse(self.bulb.state.power)
        self.assertEqual(self.bulb.state.level, 254)
        self.assertEqual(self.bulb.state.mired, 370)
        command = self.command(power=True, level=percent_to_level(40))
        self.assertTrue(self.bulb.handle(command, self.real).accepted)
        self.assertEqual(level_to_percent(self.bulb.state.level), 40)
        self.assertTrue(self.bulb.state.power)
        self.assertEqual(self.bulb.state.mired, kelvin_to_mired(2700))
        reopened = VirtualBulb(self.repo, self.real + timedelta(seconds=1))
        self.assertNotEqual(reopened.state.boot_id, self.bulb.state.boot_id)
        self.assertTrue(reopened.state.power)
        self.assertEqual(reopened.state.level, 102)

    def test_off_retains_level_and_color_change_does_not_turn_on(self):
        self.bulb.handle(self.command(level=percent_to_level(40)), self.real)
        self.bulb.handle(self.command("off", power=False), self.real)
        self.assertFalse(self.bulb.state.power)
        self.assertEqual(self.bulb.state.level, 102)
        self.bulb.handle(self.command("cool", mired=kelvin_to_mired(6500)), self.real)
        self.assertFalse(self.bulb.state.power)
        self.assertEqual(self.bulb.state.mired, 154)
        self.bulb.handle(self.command("on", power=True), self.real)
        self.assertTrue(self.bulb.state.power)
        self.assertEqual(self.bulb.state.level, 102)
        self.assertEqual(self.bulb.state.mired, 154)

    def test_transition_is_reported_and_restart_marks_interruption(self):
        command = self.command(power=True, level=102, duration_seconds=2)
        self.assertTrue(self.bulb.handle(command, self.real).accepted)
        self.assertTrue(self.bulb.state.transitioning)
        halfway = self.bulb.tick(self.real + timedelta(seconds=1))
        self.assertTrue(halfway.power)
        self.assertTrue(halfway.transitioning)
        self.assertLess(halfway.level, 102)
        restarted = VirtualBulb(self.repo, self.real + timedelta(seconds=1))
        self.assertFalse(restarted.state.transitioning)
        self.assertIn("中斷", restarted.state.fault)
        self.assertEqual(restarted.state.level, halfway.level)

    def test_duplicate_conflict_stale_version_and_expiration(self):
        first = self.command(power=True, level=102)
        result = self.bulb.handle(first, self.real)
        self.assertTrue(result.accepted)
        self.assertEqual(self.bulb.handle(first, self.real), result)
        self.assertFalse(self.bulb.handle(replace(first, level=254), self.real).accepted)
        self.assertFalse(self.bulb.handle(self.command("stale", expected_state_version=0, power=False), self.real).accepted)
        self.assertFalse(self.bulb.handle(self.command("expired", expires_at=self.real, power=False), self.real).accepted)
        self.assertTrue(self.bulb.state.power)
        self.assertEqual(self.bulb.state.level, 102)

    def test_invalid_values_are_rejected(self):
        for value in (float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                percent_to_level(value)
        with self.assertRaises(ValueError):
            self.command(level=0)
        with self.assertRaises(ValueError):
            self.command(mired=100)

    def test_controller_waits_for_final_state_and_keeps_request_idempotent(self):
        shared = Repository(self.directory / "control.db")
        transport = FakeTransport()
        control = BulbController(shared, transport)
        shared.save_device(self.bulb.state)
        payload = {"device_id": "bulb-1", "brightness_percent": 40, "kelvin": 2700,
                   "duration_seconds": 2}
        shared.submit("request-40", "set_bulb", payload)
        shared.submit("request-40", "set_bulb", payload)
        control.tick(self.real)
        self.assertEqual(len(transport.sent), 1)
        command = transport.sent[0][1]
        self.assertEqual(command.level, 102)
        self.assertEqual(shared.request("request-40")["status"], "pending")
        transport.inbox.append(("result", self.bulb.handle(command, self.real)))
        transport.inbox.append(("state", self.bulb.tick(self.real + timedelta(seconds=1))))
        control.tick(self.real + timedelta(seconds=1))
        self.assertEqual(shared.bulb_action("request-40")["status"], "accepted")
        self.assertEqual(shared.request("request-40")["status"], "pending")
        transport.inbox.append(("state", self.bulb.tick(self.real + timedelta(seconds=2))))
        control.tick(self.real + timedelta(seconds=2))
        self.assertEqual(shared.bulb_action("request-40")["status"], "confirmed")
        self.assertEqual(shared.request("request-40")["status"], "done")

    def test_controller_rejects_stale_feedback_instead_of_claiming_success(self):
        shared = Repository(self.directory / "control.db")
        transport = FakeTransport()
        control = BulbController(shared, transport)
        shared.save_device(self.bulb.state)
        shared.submit("request-offline", "set_bulb", {"device_id": "bulb-1", "power": True})
        control.tick(self.real + timedelta(seconds=11))
        self.assertEqual(shared.request("request-offline")["status"], "error")
        self.assertEqual(transport.sent, [])

    def test_late_rejection_cannot_downgrade_a_confirmed_action(self):
        shared = Repository(self.directory / "control.db")
        transport = FakeTransport()
        control = BulbController(shared, transport)
        shared.save_device(self.bulb.state)
        shared.submit("stable", "set_bulb", {"device_id": "bulb-1", "power": True,
                                                "brightness_percent": 40, "duration_seconds": 0})
        control.tick(self.real)
        command = transport.sent[0][1]
        accepted = self.bulb.handle(command, self.real)
        transport.inbox.extend([("result", accepted), ("state", self.bulb.tick(self.real))])
        control.tick(self.real)
        self.assertEqual(shared.bulb_action("stable")["status"], "confirmed")
        transport.inbox.append(("result", replace(accepted, accepted=False, message="晚到的衝突回覆")))
        control.tick(self.real + timedelta(seconds=1))
        self.assertEqual(shared.bulb_action("stable")["status"], "confirmed")
        self.assertEqual(shared.request("stable")["status"], "done")

    def test_confirmation_timeout_is_an_error_not_a_false_success(self):
        shared = Repository(self.directory / "control.db")
        transport = FakeTransport()
        control = BulbController(shared, transport)
        shared.save_device(self.bulb.state)
        shared.submit("unconfirmed", "set_bulb", {"device_id": "bulb-1", "power": True,
                                                     "duration_seconds": 0})
        control.tick(self.real)
        control.tick(self.real + timedelta(seconds=7))
        self.assertEqual(shared.bulb_action("unconfirmed")["status"], "unknown")
        self.assertEqual(shared.request("unconfirmed")["status"], "error")
        self.assertIn("未確認", shared.request("unconfirmed")["result"])


if __name__ == "__main__":
    unittest.main()

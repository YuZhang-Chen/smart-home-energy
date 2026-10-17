from datetime import timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest

from smart_home_energy.domain import TaskConstraints, WashProgram, Tariff, find_schedule
from smart_home_energy.repository import Repository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.repo = Repository(Path(directory) / "test.sqlite3")
        self.clock = self.repo.clock()
        now = self.clock.now()
        self.c = TaskConstraints(now + timedelta(minutes=2), now + timedelta(hours=12), None, None)
        self.p, self.t = WashProgram(), Tariff()

    def proposed(self):
        task = self.repo.create_task(self.c, self.p, self.t)
        plan = find_schedule(self.c, self.p, self.t, self.clock.now(), self.clock.clock_id)
        return self.repo.propose(task.task_id, task.version, self.c, plan)

    def test_single_task_version_approval_and_recovery(self):
        task = self.proposed()
        with self.assertRaises(ValueError):
            self.repo.create_task(self.c, self.p, self.t)
        with self.assertRaises(ValueError):
            self.repo.approve(task.task_id, task.version - 1, task.plan.cost)
        task = self.repo.approve(task.task_id, task.version, task.plan.cost)
        reopened = Repository(self.repo.path)
        self.assertEqual(reopened.active_task(), task)
        self.assertEqual(reopened.clock(), self.clock)
        with self.assertRaises(ValueError):
            self.repo.switch_clock(60)
        task = self.repo.update_task(task.task_id, task.version, status="failed")
        with self.assertRaises(ValueError):
            self.repo.create_task(self.c, self.p, self.t)

    def test_reschedule_preserves_approval_or_requires_new(self):
        task = self.proposed()
        task = self.repo.approve(task.task_id, task.version, 10)
        plan = find_schedule(self.c, self.p, self.t, self.clock.now(), self.clock.clock_id)
        updated = self.repo.propose(task.task_id, task.version, self.c, plan, True, "測試改排")
        self.assertEqual(updated.status, "scheduled")
        extended = TaskConstraints(self.c.earliest + timedelta(days=1), self.c.deadline + timedelta(days=1), None, None)
        plan = find_schedule(extended, self.p, self.t, self.clock.now(), self.clock.clock_id)
        updated = self.repo.propose(updated.task_id, updated.version, extended, plan, True)
        self.assertEqual(updated.status, "pending")
        self.assertIsNone(updated.approval)

    def test_request_idempotency_and_event_coalescing(self):
        self.repo.submit("req", "create", {"text": "洗衣"})
        self.repo.submit("req", "create", {"text": "洗衣"})
        self.assertEqual(len(self.repo.pending_requests()), 1)
        with self.assertRaises(ValueError):
            self.repo.submit("req", "cancel", {})
        for _ in range(4):
            self.repo.event("offline", None, {})
        event = self.repo.claim_event()
        self.assertIsNotNone(event)
        self.assertIsNone(self.repo.claim_event())
        self.repo.recover_events()
        self.assertEqual(self.repo.claim_event().event_id, event.event_id)


if __name__ == "__main__":
    unittest.main()

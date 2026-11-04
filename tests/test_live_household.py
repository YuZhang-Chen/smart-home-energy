"""Opt-in real-model tool/delegation test; devices here are telemetry fixtures."""
from dataclasses import replace
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import uuid4

from smart_home_energy.ac import ACState
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import utcnow
from smart_home_energy.multi_agent import run_household
from smart_home_energy.repository import Repository


@unittest.skipUnless(os.getenv("RUN_AGENT_INTEGRATION_TESTS", "").lower() in {"1", "true", "yes"},
                     "Set RUN_AGENT_INTEGRATION_TESTS=1 for actual model requests")
class LiveHouseholdTests(unittest.TestCase):
    def test_model_delegates_and_keeps_each_device_behind_approval(self):
        with TemporaryDirectory() as temporary:
            repo = Repository(Path(temporary) / "live.db")
            hems = HemsSettings.from_env()
            clock, real = repo.clock(), utcnow()
            state = ACState(uuid4().hex, 1, real, clock.now(real), clock.clock_id)
            stopped = threading.Event()

            def sensor():
                seq = 1
                while not stopped.is_set():
                    seq += 1
                    repo.save_device(replace(state, sequence=seq, observed_at=utcnow(), logical_at=clock.now()))
                    stopped.wait(.5)

            repo.save_device(state)
            worker = threading.Thread(target=sensor, daemon=True)
            worker.start()
            text = "請安排冷氣運轉一小時、目標27°C；洗衣請安排在24小時內完成並避開23:00–07:00。兩台設備各自提出待核准計畫，不要核准或啟動設備。"
            repo.event("live:" + uuid4().hex, None, {"text": text, "root_id": uuid4().hex})
            try:
                run = asyncio.run(run_household(repo, repo.claim_event(), hems))
                runs = [json.loads(row["data"]) for row in repo.records("agent_runs", 20)]
                summary = {"status": run.status, "provider": run.provider, "requests": run.requests,
                    "elapsed": run.elapsed,
                    "agent_runs": [{key: row.get(key) for key in ("role", "device_id", "status", "requests", "message")}
                                   for row in runs],
                    "tasks": [{"device_id": t.device_id, "status": t.status,
                               "has_plan": bool(t.plan or t.ac_plan)} for t in repo.active_tasks()]}
                output = Path(__file__).resolve().parents[1] / "artifacts/live-household.json"
                output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
                self.assertIn(run.status, {"completed", "partial_success"}, run.message)
                self.assertEqual({t.device_id for t in repo.active_tasks()}, {"washer-1", "ac-1"})
                self.assertTrue(all(t.status in {"pending", "needs_details"} for t in repo.active_tasks()))
                self.assertTrue(any(t.plan or t.ac_plan for t in repo.active_tasks()))
                self.assertEqual(repo.records("commands"), [])
                self.assertLessEqual(run.requests, 15)
                print(json.dumps(summary), flush=True)
            finally:
                stopped.set()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

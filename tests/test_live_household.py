"""Opt-in real-model tool/delegation test; devices here are telemetry fixtures."""
from dataclasses import replace
from datetime import timedelta
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
from smart_home_energy.domain import TAIPEI, utcnow
from smart_home_energy.multi_agent import run_household
from smart_home_energy.repository import Repository


@unittest.skipUnless(os.getenv("RUN_AGENT_INTEGRATION_TESTS", "").lower() in {"1", "true", "yes"},
                     "Set RUN_AGENT_INTEGRATION_TESTS=1 for actual model requests")
class LiveHouseholdTests(unittest.TestCase):
    def test_model_delegates_and_proposes_both_devices(self):
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
            start = (clock.now().astimezone(TAIPEI) + timedelta(hours=2)).replace(second=0, microsecond=0)
            end = start + timedelta(hours=3)
            text = f"請委派洗衣和冷氣各提出一份待核准計畫，不要執行設備。洗衣最早 {start.isoformat()} 開始，最晚 {end.isoformat()} 洗完，沒有安靜時段限制（quiet_start=null,quiet_end=null）。冷氣從 {start.isoformat()} 到 {end.isoformat()}，目標27°C、舒適26–28°C、環境32°C、回溫1800秒，到時才開機。各設備獨立提出計畫即可。"
            repo.event("live:" + uuid4().hex, None, {"text": text, "root_id": uuid4().hex})
            try:
                run = asyncio.run(run_household(repo, repo.claim_event(), hems))
                summary = {"status": run.status, "provider": run.provider, "requests": run.requests,
                    "elapsed": run.elapsed, "tasks": [{"device_id": t.device_id, "status": t.status} for t in repo.active_tasks()]}
                output = Path(__file__).resolve().parents[1] / "artifacts/live-household.json"
                output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
                self.assertEqual(run.status, "completed", run.message)
                self.assertEqual({t.device_id for t in repo.active_tasks()}, {"washer-1", "ac-1"})
                self.assertTrue(all(t.status == "pending" for t in repo.active_tasks()))
                self.assertIsNotNone(repo.active_task().plan)
                self.assertIsNotNone(repo.active_task("ac-1").ac_plan)
                self.assertEqual(repo.records("commands"), [])
                self.assertLessEqual(run.requests, 15)
                print(json.dumps(summary), flush=True)
            finally:
                stopped.set()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

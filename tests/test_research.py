import csv
import json
import os
from pathlib import Path
import subprocess
import sys

from smart_home_energy.research import percentile, summarize, run, SCENARIOS


def test_statistics_and_empty_samples():
    assert percentile([], .95) is None
    assert percentile(list(range(1, 11)), .5) == 5
    assert percentile(list(range(1, 11)), .95) == 10
    assert summarize([]) == []
    row = summarize([dict(scenario="unavailable", status="not_executed", duration_ms=None)])[0]
    assert row["samples"] == 0 and row["success_rate"] is None and row["p50_ms"] is None


def test_failure_csv_escaping_and_unique_outputs(tmp_path):
    name = 'failure,"quoted"\ncase'
    def fail(_):
        raise RuntimeError("expected fixture failure")
    first, passed = run(tmp_path, 1, [name], fail)
    second, _ = run(tmp_path, 1, [])
    assert not passed and first != second
    doc = json.loads((first / "results.json").read_text())
    assert doc["scope_status"] == "failed" and doc["overall_acceptance"] == "incomplete"
    with (first / "summary.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = next(r for r in rows if r["scenario"] == name)
    assert row["failed"] == "1" and row["success_rate"] == "0.0"
    assert any(r["status"] == "known_issue" for r in doc["results"])


def test_cli_offline_default_ten_repetitions(tmp_path):
    code = ('import socket; socket.socket=lambda *a,**k: (_ for _ in ()).throw(RuntimeError("network forbidden")); '
            'from smart_home_energy.research import main; raise SystemExit(main())')
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    completed = subprocess.run([sys.executable, "-c", code, "--output", str(tmp_path)], env=env,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    directory = next(tmp_path.iterdir())
    doc = json.loads((directory / "results.json").read_text())
    executed = [r for r in doc["results"] if r["status"] in ("passed", "failed")]
    assert len(executed) == len(SCENARIOS) * 10
    assert all(sum(r["scenario"] == s for r in executed) == 10 for s in SCENARIOS)
    assert all(r["model_requests"] is None and r["device_confirmation_ms"] is None for r in executed)
    assert doc["metadata"]["transport"] == "not_used"
    assert doc["scope_status"] == "passed" and doc["overall_acceptance"] == "incomplete"
    with (directory / "summary.csv").open(newline="", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))
    assert sum(int(r["samples"]) for r in csv_rows) == len(executed)
    assert "overall acceptance: incomplete" in (directory / "report.md").read_text(encoding="utf-8")

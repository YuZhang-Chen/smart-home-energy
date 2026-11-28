"""Tests for TVController durable requests, SQLite operation logs, and TV Agent sessions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from smart_home_energy import tv
from smart_home_energy.agent import ProviderUnavailable
from smart_home_energy.api import create_app
from smart_home_energy.config import HemsSettings
from smart_home_energy.multi_agent import SharedBudget
from smart_home_energy.repository import Repository
from smart_home_energy.tv_control import TVController


@pytest.fixture
def isolated_tv(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "DATA_FILE", tmp_path / "tv_state.json")
    with tv.lock:
        tv.state.update(
            {
                "device_id": "tv-1",
                "power": False,
                "volume": 25,
                "muted": False,
                "input": "HDMI1",
                "playback": "stopped",
                "updated_at": datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc).isoformat(),
            }
        )
    repo = Repository(str(tmp_path / "hems.db"))
    hems = replace(HemsSettings.from_env(), database=str(tmp_path / "hems.db"))
    return repo, hems


class FailingProvider:
    name = "failing-llm"

    async def complete(self, messages, timeout=None):
        raise ProviderUnavailable("模擬模型服務暫時不可用")


def test_tv_controller_processes_durable_requests_and_logs_sqlite(isolated_tv):
    repo, hems = isolated_tv
    app = create_app(repo=repo, hems=hems)
    client = TestClient(app)
    controller = TVController(repo)
    now = datetime(2026, 11, 28, 12, 5, tzinfo=timezone.utc)

    res = client.post(
        "/api/requests",
        json={
            "request_id": "req-tv-1",
            "kind": "tv_control",
            "payload": {
                "device_id": "tv-1",
                "power": True,
                "input": "HDMI2",
                "volume": 42,
                "playback": "playing",
            },
        },
    )
    assert res.status_code == 202

    controller.tick(now)

    req = repo.request("req-tv-1")
    assert req is not None
    assert req["status"] == "done"
    assert "開啟電視" in req["result"]
    assert "HDMI2" in req["result"]

    saved_device = repo.device("tv-1")
    assert saved_device is not None
    assert saved_device["power"] is True
    assert saved_device["input"] == "HDMI2"
    assert saved_device["volume"] == 42
    assert saved_device["muted"] is False
    assert saved_device["playback"] == "playing"

    commands = [json.loads(r["data"]) for r in repo.records("commands", 20)]
    tv_cmds = [c for c in commands if c.get("device_id") == "tv-1"]
    assert len(tv_cmds) == 1
    assert tv_cmds[0]["authorization_ref"] == "request:req-tv-1"

    audits = [json.loads(r["data"]) for r in repo.records("audit", 20) if r["kind"] == "tv_control"]
    assert len(audits) == 1
    assert audits[0]["before"]["power"] is False
    assert audits[0]["after"]["input"] == "HDMI2"
    assert audits[0]["after"]["volume"] == 42

    # 靜音與關機驗證
    res_mute = client.post(
        "/api/requests",
        json={"request_id": "req-tv-2", "kind": "tv_control", "payload": {"device_id": "tv-1", "muted": True}},
    )
    assert res_mute.status_code == 202
    controller.tick(now)
    assert repo.device("tv-1")["muted"] is True

    res_off = client.post(
        "/api/requests",
        json={"request_id": "req-tv-3", "kind": "tv_control", "payload": {"device_id": "tv-1", "power": False}},
    )
    assert res_off.status_code == 202
    controller.tick(now)
    assert repo.device("tv-1")["power"] is False
    assert repo.device("tv-1")["playback"] == "stopped"


def test_tv_control_request_validation(isolated_tv):
    repo, hems = isolated_tv
    client = TestClient(create_app(repo=repo, hems=hems))

    empty = client.post(
        "/api/requests",
        json={"request_id": "req-empty", "kind": "tv_control", "payload": {"device_id": "tv-1"}},
    )
    assert empty.status_code == 422

    bad_vol = client.post(
        "/api/requests",
        json={"request_id": "req-bad-vol", "kind": "tv_control", "payload": {"device_id": "tv-1", "volume": 150}},
    )
    assert bad_vol.status_code == 422

    bad_hdmi = client.post(
        "/api/requests",
        json={"request_id": "req-bad-hdmi", "kind": "tv_control", "payload": {"device_id": "tv-1", "input": "HDMI9"}},
    )
    assert bad_hdmi.status_code == 422


def test_tv_session_deterministic_and_llm_fallback_log_tools_and_audit(isolated_tv):
    repo, hems = isolated_tv
    event = SimpleNamespace(event_id="evt-1", payload={"text": "打開電視切到 HDMI 3 並把音量調到 35"})
    budget = SharedBudget(hems, [])

    run = asyncio.run(tv.tv_session(repo, event, budget, "parent-1", "root-1"))
    assert run["status"] == "completed"
    assert run["provider"] == "deterministic-tv-agent"
    assert repo.device("tv-1")["power"] is True
    assert repo.device("tv-1")["input"] == "HDMI3"
    assert repo.device("tv-1")["volume"] == 35

    # LLM 不可用時降級到 deterministic-tv-fallback 並補寫 tool_logs 與 audit
    event2 = SimpleNamespace(event_id="evt-2", payload={"text": "幫我靜音"})
    failing_budget = SharedBudget(hems, [FailingProvider()])
    run2 = asyncio.run(tv.tv_session(repo, event2, failing_budget, "parent-2", "root-2"))
    assert run2["status"] == "completed"
    assert run2["provider"] == "deterministic-tv-fallback"
    assert repo.device("tv-1")["muted"] is True

    tool_logs = [json.loads(r["data"]) for r in repo.records("tool_logs", 20)]
    assert any(log.get("provider") == "deterministic-tv-fallback" and log.get("device_id") == "tv-1" for log in tool_logs)

    audits = [json.loads(r["data"]) for r in repo.records("audit", 20) if r["kind"] == "tv_control"]
    assert len(audits) >= 2


def test_tv_tools_scope_and_auto_power_on(isolated_tv):
    repo, _ = isolated_tv
    tools = tv.TVTools(repo, "root-scope", "切換 HDMI 4")

    with pytest.raises(ValueError, match="只能控制 tv-1"):
        tools.control_tv(device_id="ac-1", power=True)

    result = tools.control_tv(input="HDMI4", volume=30)
    assert result["status"]["power"] is True
    assert result["status"]["input"] == "HDMI4"
    assert result["status"]["volume"] == 30
    assert repo.device("tv-1")["input"] == "HDMI4"


def test_compat_tv_router_records_sqlite_audit(isolated_tv):
    repo, hems = isolated_tv
    client = TestClient(create_app(repo=repo, hems=hems))

    res = client.post("/api/tv/control", json={"power": True, "input": "HDMI2", "volume": 28})
    assert res.status_code == 200
    assert res.json()["input"] == "HDMI2"

    audits = [json.loads(r["data"]) for r in repo.records("audit", 10) if r["kind"] == "tv_control"]
    assert len(audits) == 1
    assert audits[0]["source"] == "api_compat"
    assert audits[0]["authorization_ref"] == "api:/api/tv/control"


def test_agui_tv_and_curtain_card_serialization():
    from smart_home_energy.assistant_service import agui_event

    tv_frames = agui_event(
        {"kind": "tv_card", "tool_call_id": "tv:run-1", "device_id": "tv-1", "summary": "已切換至 HDMI2"},
        "home",
        "run-1",
    )
    assert isinstance(tv_frames, list) and len(tv_frames) == 4
    assert tv_frames[0].tool_call_name == "render_tv_card"
    assert '"device_id":"tv-1"' in tv_frames[1].delta

    curtain_frames = agui_event(
        {"kind": "curtain_card", "tool_call_id": "curtain:run-1", "device_id": "curtain-1", "summary": "已開至 50%"},
        "home",
        "run-1",
    )
    assert isinstance(curtain_frames, list) and len(curtain_frames) == 4
    assert curtain_frames[0].tool_call_name == "render_curtain_card"
    assert '"device_id":"curtain-1"' in curtain_frames[1].delta

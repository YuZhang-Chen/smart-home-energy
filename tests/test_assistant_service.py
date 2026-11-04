import json

import pytest
from fastapi.testclient import TestClient

from smart_home_energy.api import create_app
from smart_home_energy.assistant_service import event_stream, queue_run
from smart_home_energy.config import HemsSettings
from smart_home_energy.repository import Repository


def input_body(run_id="run-1", message_id="message-1", text="開冷氣"):
    return {"threadId": "home", "runId": run_id,
            "messages": [{"id": message_id, "role": "user", "content": text}],
            "tools": [], "context": []}


def test_assistant_run_is_durable_and_idempotent(tmp_path):
    repo = Repository(tmp_path / "assistant.db")
    run_id, created = queue_run(repo, input_body())
    assert created and run_id == "run-1"
    assert queue_run(repo, input_body()) == ("run-1", False)
    assert queue_run(repo, input_body(text="  開冷氣  ")) == ("run-1", False)
    assert repo.assistant_messages() == [{
        "id": "message-1", "role": "user", "content": "開冷氣",
        "created": repo.assistant_messages()[0]["created"],
    }]
    with pytest.raises(ValueError, match="內容不一致"):
        queue_run(repo, input_body(text="改成關冷氣"))
    with repo.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute("SELECT kind FROM requests WHERE id='assistant:run-1'").fetchone()[0] == "create_household"


def test_agui_sse_replays_persisted_events_and_plan_tool(tmp_path):
    repo = Repository(tmp_path / "events.db")
    queue_run(repo, input_body())
    repo.update_assistant_run("run-1", "completed", "已完成規劃")
    repo.append_assistant_event("run-1", {"kind": "finished"})
    repo.append_assistant_event("run-1", {"kind": "task_card", "tool_call_id": "plan:task:1", "task_id": "task", "version": 1, "device_id": "ac-1"})
    repo.append_assistant_event("run-1", {"kind": "content", "text": "已完成規劃"})
    repo.append_assistant_event("run-1", {"kind": "message_end"})
    repo.append_assistant_event("run-1", {"kind": "done"})
    output = "".join(event_stream(repo, "run-1"))
    events = [json.loads(line[6:]) for line in output.splitlines() if line.startswith("data: ")]
    assert events[0]["type"] == "RUN_STARTED"
    assert events[0]["protocolVersion"] == "1.0"
    assert [event["type"] for event in events].count("TOOL_CALL_START") == 1
    assert events[-1]["type"] == "RUN_FINISHED"
    assert "id: " in output


def test_v4_migration_recovers_streamlit_transcript_once(tmp_path):
    path = tmp_path / "legacy.db"
    repo = Repository(path)
    created = "2026-10-01T00:00:00+00:00"
    with repo.transaction() as db:
        db.execute("PRAGMA user_version=3")
        db.execute("INSERT INTO requests(id,kind,payload,status,result,created) VALUES (?,?,?,?,?,?)",
                   ("legacy-request", "create_household", json.dumps({"root_id": "legacy-root", "text": "安排洗衣"}), "done", "", created))
        db.execute("INSERT INTO events(id,key,task_id,payload,status) VALUES (?,?,?,?,?)",
                   ("event-1", "household:legacy-root", None, json.dumps({"root_id": "legacy-root", "text": "安排洗衣"}), "done"))
        db.execute("INSERT INTO agent_runs(id,data) VALUES (?,?)",
                   ("agent-1", json.dumps({"event_id": "event-1", "status": "completed", "message": "洗衣計畫已建立"})))
    migrated = Repository(path)
    assert [(m["role"], m["content"]) for m in migrated.assistant_messages()] == [
        ("user", "安排洗衣"), ("assistant", "洗衣計畫已建立")]
    assert Repository(path).assistant_messages() == migrated.assistant_messages()


def test_read_models_are_exposed_by_fastapi(tmp_path):
    repo = Repository(tmp_path / "api.db")
    app = create_app(repo=repo, hems=HemsSettings.from_env(), static_directory=tmp_path / "missing")
    client = TestClient(app)
    assert client.get("/api/assistant").json()["thread_id"] == "home"
    assert len(client.get("/api/energy").json()["samples"]) == 0
    settings = client.get("/api/settings").json()
    assert settings["tariff"]["plan"] == "simulated"
    assert client.get("/api/diagnostics").json()["services"]["api"] == "online"

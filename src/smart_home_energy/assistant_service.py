"""Durable single-household conversation and AG-UI event serialization."""
from __future__ import annotations

import re
import time
import json
from typing import Iterator

from smart_home_energy.domain import encode, utcnow
from smart_home_energy.repository import Repository

THREAD_ID = "home"


def input_text(body: dict) -> tuple[str, str, str]:
    """Read the final user message from an AG-UI RunAgentInput-shaped payload."""
    from ag_ui.core import RunAgentInput
    parsed = RunAgentInput.model_validate(body)
    body = parsed.model_dump(by_alias=True, exclude_none=True)
    thread_id = str(body.get("threadId") or THREAD_ID)
    run_id = str(body.get("runId") or "")
    messages = body.get("messages") or []
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", run_id):
        raise ValueError("AG-UI runId 格式不合法")
    if thread_id != THREAD_ID:
        raise ValueError("此家庭只支援單一持久化對話")
    user = next((item for item in reversed(messages) if item.get("role") == "user"), None)
    content = user.get("content") if user else None
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")
    if not isinstance(content, str) or not content.strip() or len(content) > 8000:
        raise ValueError("請輸入 1–8000 字的家庭需求")
    message_id = str((user or {}).get("id") or run_id)
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", message_id):
        raise ValueError("AG-UI message id 格式不合法")
    return thread_id, run_id, message_id


def agui_event(payload: dict, thread_id: str, run_id: str):
    from ag_ui.core import (EventType, PROTOCOL_VERSION, CustomEvent, RunErrorEvent,
                            RunFinishedEvent, RunStartedEvent, TextMessageContentEvent,
                            TextMessageEndEvent, TextMessageStartEvent, ToolCallArgsEvent,
                            ToolCallEndEvent, ToolCallResultEvent, ToolCallStartEvent)
    kind = payload.get("kind")
    if kind == "started":
        return RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id, protocol_version=PROTOCOL_VERSION)
    if kind == "progress":
        return CustomEvent(type=EventType.CUSTOM, name="assistant_progress", value={"text": payload.get("text", "正在處理需求")})
    if kind == "finished":
        message_id = f"assistant:{run_id}"
        return TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=message_id, role="assistant")
    if kind == "content":
        return TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=f"assistant:{run_id}", delta=payload.get("text", ""))
    if kind == "message_end":
        return TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=f"assistant:{run_id}")
    if kind == "error":
        return RunErrorEvent(type=EventType.RUN_ERROR, message=payload.get("text", "家庭助理工作未完成"), code="agent_failed")
    if kind == "task_card":
        tool_id = payload["tool_call_id"]
        message_id = f"assistant:{run_id}"
        args = json.dumps({"task_id": payload["task_id"], "version": payload["version"], "device_id": payload["device_id"]}, separators=(",", ":"))
        return [
            ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=tool_id, tool_call_name="render_task_card", parent_message_id=message_id),
            ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=tool_id, delta=args),
            ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tool_id),
            ToolCallResultEvent(type=EventType.TOOL_CALL_RESULT, message_id=f"tool:{tool_id}", tool_call_id=tool_id, content="此為資料庫中已保存的設備計畫；執行仍須依核准卡片操作。", role="tool"),
        ]
    if kind == "light_card":
        tool_id = payload["tool_call_id"]
        args = json.dumps({"request_id": payload["request_id"]}, separators=(",", ":"))
        return [
            ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=tool_id, tool_call_name="render_light_card", parent_message_id=f"assistant:{run_id}"),
            ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=tool_id, delta=args),
            ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tool_id),
            ToolCallResultEvent(type=EventType.TOOL_CALL_RESULT, message_id=f"tool:{tool_id}", tool_call_id=tool_id, content="燈泡動作狀態以設備回報為準。", role="tool"),
        ]
    if kind == "done":
        return RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id)
    return CustomEvent(type=EventType.CUSTOM, name="assistant_event", value=payload)


def event_stream(repo: Repository, run_id: str, after: int = 0) -> Iterator[str]:
    """Replay durable AG-UI events and tail them until this run is terminal."""
    from ag_ui.encoder import EventEncoder
    encoder = EventEncoder()
    cursor = max(0, after)
    idle = 0
    while True:
        run = repo.assistant_run(run_id)
        if run is None:
            yield "event: error\ndata: {\"detail\":\"找不到對話工作\"}\n\n"
            return
        rows = repo.assistant_events(run_id, cursor)
        for row in rows:
            cursor = row["sequence"]
            frames = agui_event(row["payload"], run["thread_id"], run_id)
            if not isinstance(frames, list):
                frames = [frames]
            for frame in frames:
                yield f"id: {cursor}\n{encoder.encode(frame)}"
        if run["status"] in {"completed", "needs_details", "partial_success", "failed"} and not rows:
            # A completed run can be reconnected from the beginning: persisted events are
            # authoritative, and this terminal marker handles upgrades of older run rows.
            if not repo.assistant_events(run_id, cursor):
                yield ": keep-alive\n\n"
                return
        idle += 1
        if idle % 60 == 0:
            yield ": keep-alive\n\n"
        time.sleep(.25)


def queue_run(repo: Repository, body: dict) -> tuple[str, bool]:
    thread_id, run_id, message_id = input_text(body)
    user = next(item for item in reversed(body.get("messages", [])) if item.get("role") == "user")
    content = user.get("content")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")
    content = content.strip()
    root_id = run_id
    existing = repo.assistant_run(run_id)
    if existing:
        if existing["message_id"] != message_id:
            raise ValueError("runId 已用於其他對話工作")
        messages = repo.assistant_messages(thread_id)
        saved = next((item for item in messages if item["id"] == message_id), None)
        if not saved or saved["content"] != content:
            raise ValueError("相同識別碼的訊息內容不一致")
        return run_id, False
    previous = repo.assistant_run_for_message(thread_id, message_id)
    if previous:
        messages = repo.assistant_messages(thread_id)
        saved = next((item for item in messages if item["id"] == message_id), None)
        if not saved or saved["content"] != content:
            raise ValueError("相同識別碼的訊息內容不一致")
        return previous["id"], False
    with repo.transaction():
        active = repo.active_task("ac-1") or repo.active_task("washer-1")
        # One conversational job at a time, while device tasks can continue in background.
        with repo.transaction() as db:
            busy = db.execute("SELECT id FROM assistant_runs WHERE status IN ('pending','running') LIMIT 1").fetchone()
        if busy:
            raise RuntimeError("家庭助理正在處理上一筆需求，請稍候再送出")
        request_id = f"assistant:{root_id}"
        repo.create_assistant_run(run_id, thread_id, message_id, root_id, content.strip())
        repo.append_assistant_event(run_id, {"kind": "started"})
        repo.append_assistant_event(run_id, {"kind": "progress", "text": "已收到需求，正在交由家庭 Agent 處理"})
        repo.submit(request_id, "create_household", {"text": content.strip(), "root_id": root_id})
    return run_id, True

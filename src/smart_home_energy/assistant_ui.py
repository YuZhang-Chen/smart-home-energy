"""Conversation and approval cards; all actions go through durable requests."""
from datetime import datetime
import json
from uuid import uuid4

import streamlit as st

from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import decode, utcnow
from smart_home_energy.hems_ui import request, local_date, LABELS
from smart_home_energy.repository import Repository, Task
from smart_home_energy.assistant_defaults import preferences

NAMES = {"ac-1": "冷氣", "washer-1": "洗衣機"}
STATUS = {**LABELS, "pending": "等你確認", "needs_details": "需要補充", "scheduled": "已預約",
          "starting": "正在啟動", "running": "運轉中", "stopping": "正在停止", "unknown": "等待設備回報"}


def readable_message(message):
    return message.replace("washer-1", "洗衣機").replace("ac-1", "冷氣").replace("委派設備或任務版本不符", "計畫剛剛有更新，請重新送出需求，我會依最新狀態安排")


def submit_need(repo, text, task=None):
    payload = {"text": text.strip()}
    if not task:
        payload["root_id"] = uuid4().hex
    identifier = request(repo, "details" if task else "create_household", payload, task, feedback=False)
    if identifier:
        st.session_state["assistant_last_request"] = identifier
        st.rerun()


def conversations(repo):
    """Reconstruct messages from persisted requests and their matching work."""
    runs = [json.loads(row["data"]) for row in repo.records("agent_runs", 200)]
    result = []
    for row in reversed(repo.records("requests", 100)):
        if row["kind"] not in {"create_household", "details"}:
            continue
        payload = json.loads(row["payload"])
        if row["kind"] == "create_household":
            key = "household:" + payload["root_id"]
        else:
            key = f"requirement:{payload['task_id']}:{payload['expected_version'] + 1}"
        with repo.transaction() as db:
            event = db.execute("SELECT id,status FROM events WHERE key=? ORDER BY rowid DESC LIMIT 1", (key,)).fetchone()
        run = next((r for r in runs if event and r["event_id"] == event["id"] and not r.get("parent_id")), None)
        pending = row["status"] == "pending" or bool(event and event["status"] in {"pending", "running"})
        result.append({"id": row["id"], "text": payload["text"], "pending": pending,
                       "run": run, "error": row["result"] if row["status"] == "error" else None})
    return result


def action_feedback(repo):
    identifier = st.session_state.get("assistant_last_request")
    if not identifier:
        return
    with repo.transaction() as db:
        row = db.execute("SELECT kind,status,result FROM requests WHERE id=?", (identifier,)).fetchone()
    if not row:
        return
    if row["status"] == "error":
        st.error(row["result"])
    elif row["status"] == "pending":
        st.info("已收到，正在處理。")
    elif row["kind"] not in {"create_household", "details"}:
        st.caption("操作已送出，以下卡片會依設備回報更新。")


def device_card(repo, task, planning=False):
    name = NAMES[task.device_id]
    state = repo.device(task.device_id)
    matching = state and state.get("task_id") == task.task_id and state.get("run_id") == task.run_id
    with st.container(border=True):
        title, badge = st.columns([3, 2])
        title.subheader(name)
        badge.markdown(f"**{'正在規劃' if planning and task.status == 'needs_details' else STATUS.get(task.status, task.status)}**")
        p = task.ac_plan if task.kind == "ac" else task.plan
        if p:
            if task.kind == "ac":
                c = p.constraints
                duration = (c.end - c.start).total_seconds() / 60
                if p.start_on_approval:
                    st.write(f"確認後開啟 · 運轉 {duration:g} 分鐘後自動關閉")
                else:
                    st.write(f"{local_date(c.start)} → {local_date(c.end)}")
                st.write(f"目標 **{c.target_c:g}°C** · 舒適範圍 {c.minimum_c:g}–{c.maximum_c:g}°C")
                st.caption(f"預估開機後 {p.comfortable_after_seconds / 60:.0f} 分鐘進入舒適範圍；費用超估會提醒，仍以舒適為優先。" if p.comfortable_after_seconds is not None else "此時段無法達到舒適範圍，請調整計畫。")
            else:
                st.write(f"{local_date(p.start)} → {local_date(p.end)}")
                st.write(f"標準洗衣 · {p.program.seconds / 60:g} 分鐘")
                if task.constraints and task.constraints.quiet_start is not None:
                    c = task.constraints
                    st.caption(f"避開安靜時段 {c.quiet_start // 60:02}:{c.quiet_start % 60:02}–{c.quiet_end // 60:02}:{c.quiet_end % 60:02}")
            label = "模擬費率估算" if p.tariff.official is None else "台電費率增量估算"
            st.caption(f"{label} NT${p.cost:.2f} · 預估用電 {p.energy_kwh:.3f} 度")
        if matching and task.status in {"starting", "running", "stopping", "unknown", "completed", "aborted", "failed"}:
            fresh = -2 <= (utcnow() - datetime.fromisoformat(state["observed_at"])).total_seconds() <= 10
            if not fresh or task.status == "unknown":
                st.warning("正在等候設備回報；目前還不能確認設備已停止。")
            a, b = st.columns(2)
            a.metric("室溫" if task.kind == "ac" else "洗衣進度", f"{state['temperature_c']:.1f}°C" if task.kind == "ac" else f"{state['progress']:.0%}")
            b.metric("本次用電", f"{state['energy_kwh']:.3f} 度")
            st.progress(state["progress"])
            if task.status == "completed":
                st.success("已依計畫關閉冷氣。" if task.kind == "ac" else "衣服已洗好。")
            elif task.status == "running" and task.kind == "ac":
                c = task.ac_plan.constraints
                st.write("已進入舒適範圍" if c.minimum_c <= state["temperature_c"] <= c.maximum_c else "正在調整室溫")
        if task.status == "needs_details" and not planning:
            st.info("可以在上方直接補充需求，或使用「修改計畫」告訴助理。")
        if task.status in {"failed", "unknown"}:
            st.warning(task.message or "請先查核設備狀態。")
        if task.status == "pending" and p:
            if st.button("確認開啟冷氣" if task.kind == "ac" else "確認安排洗衣", type="primary", key=f"confirm_{task.task_id}_{task.version}", use_container_width=True):
                identifier = request(repo, "approve", {} if task.kind == "ac" else {"max_cost": p.cost}, task)
                st.session_state["assistant_last_request"] = identifier
        if task.status in {"needs_details", "pending", "scheduled"}:
            left, right = st.columns(2)
            if left.button("修改計畫", key=f"edit_{task.task_id}", use_container_width=True):
                st.session_state["assistant_edit"] = task.task_id
            if right.button("取消計畫", key=f"cancel_{task.task_id}_{task.version}", use_container_width=True):
                st.session_state["assistant_last_request"] = request(repo, "cancel", {}, task)
            if st.session_state.get("assistant_edit") == task.task_id:
                with st.form(f"edit_plan_{task.task_id}"):
                    text = st.text_input("想怎麼修改？", placeholder="例如：改成開兩小時，或明天早上八點前洗好。", key=f"edit_text_{task.task_id}")
                    if st.form_submit_button("請助理調整"):
                        if text.strip():
                            submit_need(repo, text, task)
                            st.session_state.pop("assistant_edit", None)
                        else:
                            st.error("請輸入要調整的內容。")
        if task.status in {"running", "starting", "unknown"}:
            stop_key = "assistant_stop:" + task.task_id
            if not st.session_state.get(stop_key):
                if st.button("關閉冷氣" if task.kind == "ac" else "停止洗衣", key=f"stop_{task.task_id}", use_container_width=True):
                    st.session_state[stop_key] = True
                    st.rerun()
            else:
                st.warning("確定現在停止嗎？這次任務不會自動重跑。")
                yes, no = st.columns(2)
                if yes.button("確認停止", key=f"stop_yes_{task.task_id}_{task.version}", type="primary"):
                    st.session_state["assistant_last_request"] = request(repo, "abort", {"confirmed": True}, task)
                    st.session_state.pop(stop_key, None)
                if no.button("繼續運轉", key=f"stop_no_{task.task_id}"):
                    st.session_state.pop(stop_key, None)
                    st.rerun()
        if task.status in {"unknown", "failed"}:
            if st.button("重新查核", key=f"query_{task.task_id}"):
                st.session_state["assistant_last_request"] = request(repo, "query", {}, task)
            checked = st.checkbox("我已檢查設備，確認可以解除", key=f"reset_check_{task.task_id}")
            if st.button("解除異常", disabled=not checked, key=f"reset_{task.task_id}_{task.version}"):
                st.session_state["assistant_last_request"] = request(repo, "reset", {"confirmed": True}, task)


@st.fragment(run_every=5)
def render_assistant():
    repo = Repository(HemsSettings.from_env().database)
    history = conversations(repo)
    busy = any(item["pending"] for item in history)
    heartbeat = repo.meta("controller_heartbeat")
    if not heartbeat or (utcnow() - datetime.fromisoformat(heartbeat)).total_seconds() > 10:
        st.warning("家庭控制服務尚未連線。需求可以先保存，連線後才會處理。")
    if repo.clock().speed != 1:
        st.info(f"目前為 {repo.clock().speed:g} 倍展示時間；可在左側「偏好與進階設定」調整。")
    st.subheader("今天想讓家裡幫你做什麼？")
    st.caption("說出需求，助理幫你安排。每台設備都會先請你確認，再開始執行。")
    prefs = preferences(repo)
    st.caption(f"常用偏好：冷氣 {prefs['target_c']:g}°C、{prefs['duration_minutes']} 分鐘；洗衣 {prefs['washer_hours']} 小時內完成。")
    with st.container(border=True):
        text = st.chat_input("例如：幫我開冷氣，順便安排洗衣", key="assistant_input", disabled=busy)
        if text and text.strip():
            submit_need(repo, text)
        left, right = st.columns(2)
        if left.button("開冷氣 1 小時", key="quick_ac", use_container_width=True, disabled=busy):
            submit_need(repo, "幫我開冷氣，確認後開啟，運轉一小時，使用常用溫度偏好。")
        if right.button("安排洗衣", key="quick_washer", use_container_width=True, disabled=busy):
            submit_need(repo, "幫我安排洗衣，使用常用期限與安靜時段偏好，選預估費用較低的時段。")
    if history:
        latest = history[-1]
        with st.chat_message("user"):
            st.write(latest["text"])
        with st.chat_message("assistant"):
            if latest["pending"]:
                st.status("正在幫你整理需求與安排設備…", state="running")
            elif latest["error"]:
                st.error(latest["error"])
            elif latest["run"]:
                run = latest["run"]
                if run['status'] == 'completed' and '計畫已提出' in run['message']:
                    st.write("下方是這次需求的計畫與最新進度。每台設備各自確認即可。")
                else:
                    if run['status'] == 'partial_success':
                        st.write("部分計畫已完成，請先查看下方卡片。")
                    st.write(readable_message(run["message"]))
            else:
                st.write("已收到你的需求。")
        if len(history) > 1:
            with st.expander("先前的對話"):
                for item in history[-8:-1]:
                    st.write("你：" + item["text"])
                    if item["run"]:
                        st.write("助理：" + readable_message(item["run"]["message"]))
    action_feedback(repo)
    tasks = repo.active_tasks()
    has_active = bool(tasks)
    recent = repo.recent_tasks()
    for device in NAMES:
        if not any(t.device_id == device for t in tasks):
            last = next((r for r in recent if r.get("device_id", "washer-1") == device), None)
            if last and last['status'] in {"completed", "aborted"}:
                tasks.append(decode(Task, last))
    if tasks:
        area = st.container() if has_active else st.expander("最近一次執行")
        with area:
            if has_active:
                st.subheader("你的計畫與進度")
            columns = st.columns(2) if len(tasks) == 2 else [st.container()]
            for column, task in zip(columns, sorted(tasks, key=lambda t: t.device_id)):
                with column:
                    device_card(repo, task, planning=busy)
    if not has_active and not busy:
        st.caption("還沒有計畫。先點一個快捷按鈕，或直接告訴助理你的需求。")

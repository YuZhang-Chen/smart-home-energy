"""Streamlit submits durable requests; it never owns a scheduler or LLM worker."""

from datetime import datetime, time, timedelta
import json
import hashlib
from uuid import uuid4

import streamlit as st

from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import TAIPEI, TaskConstraints, decode, encode, utcnow
from smart_home_energy.repository import Repository
from smart_home_energy.washer import DeviceState
from smart_home_energy.control import fresh

LABELS = {"needs_details": "待補充條件", "pending": "待核准", "scheduled": "已排程", "starting": "啟動確認中",
          "running": "運轉中", "stopping": "中止確認中", "completed": "已完成", "cancelled": "已取消",
          "aborted": "已中止", "failed": "設備故障待人工處理", "unknown": "狀態未知", "resolved": "已人工解除"}


def local_date(value: datetime) -> str:
    return value.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def request(repo: Repository, kind: str, payload: dict, task=None, feedback=True):
    if task:
        payload = {**payload, "task_id": task.task_id, "expected_version": task.version}
    token = st.session_state.setdefault("hems_request_token", uuid4().hex)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    identifier = f"ui:{token}:{kind}:{digest}"
    try:
        repo.submit(identifier, kind, payload)
        st.session_state["hems_last_request"] = identifier
        if feedback:
            st.success("已送出，正在處理。")
        return identifier
    except ValueError as error:
        st.error(str(error))


def schedule_form(repo: Repository, clock, task):
    defaults = task.constraints if task and task.constraints else None
    now = clock.now().astimezone(TAIPEI)
    earliest = defaults.earliest.astimezone(TAIPEI) if defaults else now + timedelta(minutes=5)
    deadline = defaults.deadline.astimezone(TAIPEI) if defaults else now + timedelta(days=1)
    with st.form("washer_schedule"):
        st.caption("表單排程可在模型未設定或失敗時使用。時間採 Asia/Taipei，完整行程不可碰到安靜時段。")
        a, b = st.columns(2)
        start_date = a.date_input("最早開始日期", earliest.date())
        start_time = a.time_input("最早開始時間", earliest.time().replace(tzinfo=None))
        end_date = b.date_input("最晚完成日期", deadline.date())
        end_time = b.time_input("最晚完成時間", deadline.time().replace(tzinfo=None))
        quiet = st.checkbox("啟用安靜時段", value=defaults is None or defaults.quiet_start is not None)
        qs = defaults.quiet_start if defaults and defaults.quiet_start is not None else 23 * 60
        qe = defaults.quiet_end if defaults and defaults.quiet_end is not None else 7 * 60
        qstart = a.time_input("安靜時段開始", time(qs // 60, qs % 60))
        qend = b.time_input("安靜時段結束", time(qe // 60, qe % 60))
        if st.form_submit_button("計算並提出計畫", disabled=bool(task and task.status not in {"needs_details", "pending", "scheduled"})):
            try:
                c = TaskConstraints(datetime.combine(start_date, start_time, TAIPEI), datetime.combine(end_date, end_time, TAIPEI),
                                    qstart.hour * 60 + qstart.minute if quiet else None,
                                    qend.hour * 60 + qend.minute if quiet else None)
                request(repo, "schedule_form" if task else "create_form", {"constraints": encode(c)}, task)
            except ValueError as error:
                st.error(str(error))


@st.fragment(run_every=5)
def render_washer():
    st.subheader("洗衣手動設定")
    st.caption("虛擬洗衣機 · 模擬電價或台電正式費率估算（NT$），非實際帳單。關閉瀏覽器後，控制服務仍會執行已核准任務。")
    try:
        hems = HemsSettings.from_env()
        repo = Repository(hems.database)
        clock = repo.clock()
    except ValueError as error:
        st.error(f"洗衣設定錯誤：{error}")
        return
    task = repo.active_task()
    submitted = st.session_state.get("hems_last_request")
    if submitted and any(r["id"] == submitted and r["status"] != "pending" for r in repo.records("requests", 20)):
        st.session_state["hems_request_token"] = uuid4().hex
        st.session_state["hems_last_request"] = None
    current_id = task.task_id if task else None
    if st.session_state.get("hems_last_task") != current_id:
        st.session_state["hems_last_task"] = current_id
        st.session_state["hems_request_token"] = uuid4().hex
    st.caption(f"邏輯時間：{local_date(clock.now())}（Asia/Taipei）· {clock.speed:g} 倍")
    heartbeat = repo.meta("controller_heartbeat")
    if not heartbeat or (utcnow() - datetime.fromisoformat(heartbeat)).total_seconds() > 10:
        st.warning("控制服務尚未連線或心跳過期；請在 PowerShell 啟動 smart_home_energy.control。請求會保留於資料庫。")
    else:
        st.success("控制服務執行中")
    data = repo.device()
    if data:
        state = decode(DeviceState, data)
        if not fresh(state, utcnow()):
            st.warning("洗衣機資料超過 10 個真實秒未更新；保留最後數值，不能據此認定已停止。")
        st.write(f"設備：{state.status} · 階段：{state.stage}")
        if state.fault:
            st.error(state.fault)
        st.progress(state.progress, text=f"行程進度 {state.progress:.1%}")
        p, e, c = st.columns(3)
        p.metric("洗衣功率 (W)", f"{state.power_w:.1f}")
        e.metric("洗衣累積用電 (kWh)", f"{state.energy_kwh:.6f}")
        c.metric("依執行累積估算費用 (NT$)", f"{state.cost:.6f}")
        with st.expander("洗衣功率與用電紀錄"):
            samples = [json.loads(r["data"]) for r in reversed(repo.records("samples", 900))]
            matching = [r for r in samples if r["run_id"] == state.run_id and r["boot_id"] == state.boot_id]
            if matching:
                st.line_chart([{"邏輯時間": datetime.fromisoformat(r["logical_at"]).astimezone(TAIPEI), "功率 (W)": r["power_w"]} for r in matching], x="邏輯時間", y="功率 (W)")
            st.json(data)
    else:
        st.info("等待虛擬洗衣機資料。請啟動 smart_home_energy.washer。")

    if task:
        st.write(f"任務 {task.task_id[:8]} · 版本 {task.version} · {LABELS.get(task.status, task.status)}")
        if task.message:
            st.info(task.message)
        if task.plan:
            plan = task.plan
            st.write(f"計畫：{local_date(plan.start)} → {local_date(plan.end)}（Asia/Taipei）")
            st.write(f"{plan.program.name}行程 · {plan.program.seconds / 60:g} 分鐘 · {plan.energy_kwh:.6f} kWh · 預估 NT${plan.cost:.6f}")
            if plan.tariff.official is None:
                st.text(f"模擬電價：08:00–22:00 NT${plan.tariff.peak:g}/kWh；其餘 NT${plan.tariff.offpeak:g}/kWh · {plan.tariff.version}")
            else:
                st.write(f"台電正式費率下的增量估算 · {plan.tariff.version}；日曆月基準，不含固定基本費")
                with st.expander("洗衣費率與基準快照"):
                    st.json({key: value for key, value in plan.tariff.official.items() if key not in {"raw", "calendar"}})
            st.write("行程：" + "、".join(f"{s.name} {s.seconds / 60:g} 分鐘／{s.power_w:g} W" for s in plan.program.stages))
            if task.constraints:
                c = task.constraints
                quiet = "無" if c.quiet_start is None else f"{c.quiet_start // 60:02}:{c.quiet_start % 60:02}–{c.quiet_end // 60:02}:{c.quiet_end % 60:02}"
                st.write(f"允許範圍：{local_date(c.earliest)} → {local_date(c.deadline)}；每日安靜時段：{quiet}")
            if task.approval:
                st.write(f"已核准自動改排費用上限：NT${task.approval.max_cost:.9f}")
            if task.status == "pending":
                with st.form(f"approve_{task.version}"):
                    maximum = st.number_input("自動改排費用上限 (NT$)", min_value=0.0, value=float(plan.cost), format="%.9f", step=.01)
                    st.caption("核准後可在上述期限、安靜時段、同一行程及費率版本內自動改排。")
                    if st.form_submit_button("核准此版本計畫"):
                        request(repo, "approve", {"max_cost": maximum}, task)
        if task.status in {"needs_details", "pending", "scheduled"}:
            if st.button("取消未啟動任務"):
                request(repo, "cancel", {}, task)
        if task.status in {"running", "starting", "unknown"}:
            confirm = st.checkbox("我確認要中止目前洗衣行程（不會自動重跑）")
            if st.button("確認中止", disabled=not confirm):
                request(repo, "abort", {"confirmed": True}, task)
        if task.status in {"failed", "unknown"}:
            confirm_reset = st.checkbox("我已人工檢查設備，確認可以解除故障／未知狀態")
            if st.button("人工解除", disabled=not confirm_reset):
                request(repo, "reset", {"confirmed": True}, task)
        if st.button("查核設備狀態"):
            request(repo, "query", {}, task)

    with st.form("natural_language"):
        text = st.text_area("自然語言需求／補充條件", placeholder="例如：明天早上 8 點前洗完，今晚 11 點到明早 7 點保持安靜。")
        if st.form_submit_button("交給 Main Agent", disabled=bool(task and task.status not in {"needs_details", "pending", "scheduled"})):
            if text.strip():
                request(repo, "details" if task else "create_text", {"text": text.strip()}, task)
            else:
                st.error("請先輸入需求")
    with st.expander("表單排程（不需要模型）", expanded=task is None or task.status == "needs_details"):
        schedule_form(repo, clock, task)
    with st.expander("請求、Agent 與工具紀錄"):
        for r in repo.records("agent_runs", 10):
            run = json.loads(r["data"])
            st.write(f"{run['provider']} · {run['status']} · {run['requests']} 次／{run['elapsed']:.2f} 秒")
            st.write(run["message"])
        st.json(repo.records("requests", 10))
        st.json(repo.records("tool_logs", 30))
    with st.expander("歷史任務與計畫版本"):
        st.json(repo.recent_tasks())
        st.json(repo.records("plans", 20))

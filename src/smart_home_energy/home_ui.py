"""Secondary device controls and settings, away from the assistant's main flow."""
from datetime import datetime, time, timedelta
import json
from urllib.error import URLError

import streamlit as st

from smart_home_energy.ac_domain import ACConstraints
from smart_home_energy.assistant_defaults import preferences
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import TAIPEI, encode, utcnow
from smart_home_energy.hems_ui import request
from smart_home_energy.repository import Repository
from smart_home_energy.tariffs import source_snapshot, refresh_source, household_estimate


@st.fragment(run_every=5)
def render_ac():
    repo = Repository(HemsSettings.from_env().database)
    clock, task, state = repo.clock(), repo.active_task("ac-1"), repo.device("ac-1")
    st.subheader("冷氣")
    if state:
        a, b, c = st.columns(3)
        a.metric("室溫 (°C)", f"{state['temperature_c']:.1f}")
        b.metric("冷氣功率 (W)", f"{state['power_w']:.0f}")
        c.metric("本任務用電 (kWh)", f"{state['energy_kwh']:.3f}")
        st.caption("運轉中" if state["status"] == "running" else "冷氣已關閉" if state["status"] in {"idle", "completed", "aborted"} else "需要查核設備")
        if not -2 <= (utcnow() - datetime.fromisoformat(state["observed_at"])).total_seconds() <= 10:
            st.warning("設備回報過期，正在等候連線。")
    else:
        st.info("正在等候冷氣連線。")
    st.caption("開啟、核准及關閉操作，都在上方的助理計畫卡。也可以在下方自行指定預約條件。")
    with st.expander("手動預約冷氣"):
        conditions = task.ac_constraints if task else None
        default_key = f"ac_manual_default:{clock.clock_id}:{task.task_id if task else 'new'}"
        default = st.session_state.setdefault(default_key, clock.now().astimezone(TAIPEI) + timedelta(minutes=10))
        start = conditions.start.astimezone(TAIPEI) if conditions else default
        end = conditions.end.astimezone(TAIPEI) if conditions else start + timedelta(hours=1)
        prefs = preferences(repo)
        with st.form("ac_schedule"):
            a, b = st.columns(2)
            sd = a.date_input("冷氣開始日期", start.date())
            ss = a.time_input("冷氣開始時間", start.time().replace(tzinfo=None))
            ed = b.date_input("冷氣結束日期", end.date())
            es = b.time_input("冷氣結束時間", end.time().replace(tzinfo=None))
            target = st.number_input("目標溫度", 16.0, 30.0, float(conditions.target_c if conditions else prefs['target_c']), .5)
            low = a.number_input("舒適下限", 16.0, 30.0, float(conditions.minimum_c if conditions else prefs['minimum_c']), .5)
            high = b.number_input("舒適上限", 16.0, 30.0, float(conditions.maximum_c if conditions else prefs['maximum_c']), .5)
            ambient = st.number_input("教學模型環境溫度", -20.0, 50.0, float(conditions.ambient_c if conditions else 32), .5)
            warming = st.number_input("關機回溫時間常數（分鐘）", 1.0, 240.0, float(conditions.warming_seconds / 60 if conditions else 30), 1.0)
            if st.form_submit_button("計算冷氣計畫", disabled=bool(task and task.status not in {"needs_details", "pending", "scheduled"})):
                try:
                    c = ACConstraints(datetime.combine(sd, ss, TAIPEI), datetime.combine(ed, es, TAIPEI), target, low, high, ambient, warming * 60)
                    st.session_state["assistant_last_request"] = request(repo, "schedule_ac_form" if task else "create_ac_form", {"device_id": "ac-1", "constraints": encode(c)}, task)
                except ValueError as error:
                    st.error(str(error))


@st.fragment(run_every=5)
def render_energy():
    hems = HemsSettings.from_env()
    repo = Repository(hems.database)
    st.subheader("用電紀錄")
    try:
        st.metric("家庭核准負載的增量估算", f"NT${household_estimate(repo, hems.tariff()):.2f}")
    except (ValueError, OSError) as error:
        st.warning(str(error))
    st.caption("未完成計畫使用預估，已結束計畫使用回報用電。正式費率依日曆月基準計算流動電費增量，不含固定基本費，並非實際帳單。")
    with st.expander("各次任務紀錄"):
        for row in repo.recent_tasks():
            p = row.get("ac_plan") or row.get("plan")
            if not p:
                continue
            name = "冷氣" if row.get("kind") == "ac" else "洗衣機"
            st.write(f"{name} · 預估 {p['energy_kwh']:.3f} 度 · NT${p['cost']:.2f}")


@st.fragment(run_every=5)
def render_settings():
    hems = HemsSettings.from_env()
    repo = Repository(hems.database)
    with st.expander("偏好與進階設定"):
        st.caption("虛擬設備展示。這裡的設定不會改動已核准計畫。")
        prefs = preferences(repo)
        with st.form("assistant_preferences"):
            st.write("常用偏好")
            target = st.number_input("預設冷氣溫度 (°C)", 16.0, 30.0, float(prefs['target_c']), .5)
            duration = st.number_input("預設冷氣運轉分鐘", 15, 1440, prefs['duration_minutes'], 15)
            low = st.number_input("預設舒適下限 (°C)", 16.0, 30.0, float(prefs['minimum_c']), .5)
            high = st.number_input("預設舒適上限 (°C)", 16.0, 30.0, float(prefs['maximum_c']), .5)
            hours = st.number_input("洗衣預設幾小時內完成", 1, 24, prefs['washer_hours'])
            qs = st.time_input("預設安靜時段開始", time(prefs['quiet_start'] // 60, prefs['quiet_start'] % 60))
            qe = st.time_input("預設安靜時段結束", time(prefs['quiet_end'] // 60, prefs['quiet_end'] % 60))
            if st.form_submit_button("儲存常用偏好"):
                request(repo, "assistant_preferences", {"target_c": target, "duration_minutes": duration, "minimum_c": low,
                    "maximum_c": high, "washer_hours": hours, "quiet_start": qs.hour * 60 + qs.minute, "quiet_end": qe.hour * 60 + qe.minute})
        st.divider()
        config = repo.meta("tariff_settings", {"plan": "simulated", "baseline_kwh": {}})
        names = {"simulated": "模擬電價", "residential": "台電住宅非時間", "tou2": "台電簡易二段式", "tou3": "台電簡易三段式"}
        # Keep the month selector outside the form: selecting a different month
        # must load its own baseline before the user edits and saves the number.
        month_date = st.date_input("基準用電所屬月份", repo.clock().now().astimezone(TAIPEI).date().replace(day=1))
        with st.form("tariff_profile"):
            selection = st.selectbox("電價方案", list(names), index=list(names).index(config['plan']), format_func=names.get)
            month = month_date.strftime("%Y-%m")
            baseline = st.number_input("該月基準用電（度）", min_value=0.0, value=float(config['baseline_kwh'][month]) if month in config['baseline_kwh'] else None, step=10.0,
                                       help="不含本系統任務的整月用電估計。跨月計畫須分別儲存兩個月份。", key=f"baseline_{month}")
            st.caption("先選月份，再輸入該月度數；新增月份會保留先前月份。")
            if st.form_submit_button("儲存費率設定"):
                if selection != 'simulated' and baseline is None:
                    st.error(f"請先輸入 {month} 不含本系統任務的基準用電。")
                else:
                    values = {**config['baseline_kwh'], **({month: baseline} if baseline is not None else {})}
                    request(repo, "tariff_settings", {"plan": selection, "baseline_kwh": values})
        source = repo.meta("tariff_source") or source_snapshot()
        st.caption(f"費率實施 {source['effective_from']} · 查核 {source['retrieved_at']}")
        if st.button("下載並查核台電費率"):
            try:
                st.success("已更新正式來源：" + refresh_source(repo))
            except (URLError, OSError, ValueError, KeyError) as error:
                st.error(f"更新失敗，保留既有來源：{error}")
        st.divider()
        clock = repo.clock()
        st.caption("目前系統時間：" + clock.now().astimezone(TAIPEI).strftime("%m/%d %H:%M"))
        speed = st.selectbox("時間倍率", [1, 60], index=0 if clock.speed == 1 else 1, disabled=bool(repo.active_tasks()),
                             format_func=lambda v: "真實時間（1 倍）" if v == 1 else "展示（60 倍）")
        if st.button("套用時間模式", disabled=bool(repo.active_tasks()) or speed == clock.speed):
            request(repo, "clock", {"speed": speed})
        st.caption("沒有未完成任務時才能切換時間倍率。")
        for device, name in (("ac-1", "冷氣"), ("washer-1", "洗衣機")):
            link = repo.meta("transport:" + device, {})
            st.caption(f"{name}傳輸：{link.get('kind', '等待連線')} · {'已連線' if link.get('connected') else '未連線'}")
        with st.expander("Agent 與工具紀錄"):
            st.json(repo.records("agent_runs", 20))
            st.json(repo.records("requests", 20))
            st.json(repo.records("tool_logs", 30))

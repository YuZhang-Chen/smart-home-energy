"""Streamlit only renders immutable snapshots; MQTT never calls Streamlit."""

import atexit
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import streamlit as st

from smart_home_energy.config import Settings
from smart_home_energy.mqtt_transport import TelemetryClient
from smart_home_energy.store import TelemetryStore

TAIPEI = ZoneInfo("Asia/Taipei")


@st.cache_resource(show_spinner=False)
def get_subscriber(settings: Settings) -> TelemetryClient:
    subscriber = TelemetryClient(settings, TelemetryStore())
    subscriber.start()
    atexit.register(subscriber.stop)
    return subscriber


@st.fragment(run_every=5)
def render_monitor(subscriber: TelemetryClient) -> None:
    assert subscriber.store is not None
    snapshot = subscriber.store.snapshot()
    latest = snapshot.latest

    if snapshot.connected:
        st.success("MQTT Broker 已連線")
    else:
        st.warning("MQTT Broker 尚未連線，正在自動重連。")

    temperature, power, energy = st.columns(3)
    temperature.metric("室內溫度 (°C)", f"{latest.temperature_c:.2f}" if latest else "—")
    power.metric("冷氣即時功率 (W)", f"{latest.power_w:.1f}" if latest else "—")
    energy.metric("本次累積用電 (kWh)", f"{latest.energy_kwh:.6f}" if latest else "—")

    if latest is None:
        st.info("等待模擬器資料。請在另一個 PowerShell 視窗啟動 publisher。")
        return

    updated = latest.timestamp.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    st.caption(f"最後資料時間：{updated}（Asia/Taipei）")
    if snapshot.is_stale(datetime.now(UTC)):
        st.warning("資料已超過 10 秒未更新，下方保留最後收到的數值。")

    st.subheader("冷氣功率趨勢")
    st.line_chart(
        [
            {"時間": sample.timestamp.astimezone(TAIPEI), "功率 (W)": sample.power_w}
            for sample in snapshot.samples
        ],
        x="時間", y="功率 (W)",
    )
    st.caption("最多保留最近 900 筆資料，正常更新時約為 30 分鐘。")
    with st.expander("最新 MQTT 資料"):
        st.json(latest.to_dict())


def main() -> None:
    st.set_page_config(page_title="家庭助理", page_icon="⚡", layout="wide", initial_sidebar_state="collapsed")
    st.markdown("""<style>
    [data-testid="stMainBlockContainer"] { max-width:1100px; padding-top:2rem; }
    h1 { font-size:2rem !important; letter-spacing:.02em; }
    h2,h3 { letter-spacing:.01em; }
    [data-testid="stAppDeployButton"] { display:none; }
    [data-testid="stChatMessage"] { border-radius:14px; }
    [data-testid="stBaseButton-primary"] { background:#0f766e; border-color:#0f766e; }
    [data-testid="stBaseButton-primary"]:hover { background:#115e59; border-color:#115e59; }
    button:focus-visible { outline:3px solid #0d9488 !important; outline-offset:3px; }
    [data-baseweb="tab"][aria-selected="true"] { color:#0f766e; }
    [data-baseweb="tab-highlight"] { background-color:#0f766e; }
    </style>""", unsafe_allow_html=True)
    st.title("家庭助理")
    try:
        settings = Settings.from_env()
    except ValueError as error:
        st.error(f"設定錯誤：{error}")
        st.stop()
    from smart_home_energy.assistant_ui import render_assistant
    from smart_home_energy.home_ui import render_ac, render_energy, render_settings
    with st.sidebar:
        st.header("設定")
        render_settings()
        st.caption("家電為教學用虛擬設備。費用為估算，並非實際帳單。")
    render_assistant()
    st.divider()
    ac_tab, washer_tab, energy_tab = st.tabs(["冷氣", "洗衣機", "用電紀錄"])
    with ac_tab:
        render_ac()
    with washer_tab:
        from smart_home_energy.hems_ui import render_washer
        st.subheader("洗衣機")
        st.caption("說「幫我洗衣」即可安排；計畫確認與進度會出現在上方。需要自行指定條件時，再展開手動設定。")
        with st.expander("手動設定洗衣"):
            render_washer()
    with energy_tab:
        render_energy()
        render_monitor(get_subscriber(settings))


if __name__ == "__main__":
    main()

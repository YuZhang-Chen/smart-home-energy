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


@st.fragment(run_every=2)
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
    st.set_page_config(page_title="智慧家庭能源監測", page_icon="⚡", layout="wide")
    st.title("智慧家庭能源監測")
    st.caption("冷氣即時監測 · 洗衣排程、核准與執行查核")
    st.info(
        "教學用模擬模型：冷氣持續開啟，目標 27°C，功率範圍 100–1,200 W。"
        "數據不代表實際家電；累積用電自模擬器本次啟動起計算。"
    )
    try:
        settings = Settings.from_env()
    except ValueError as error:
        st.error(f"設定錯誤：{error}")
        st.stop()
    st.caption(f"Broker：{settings.mqtt_host}:{settings.mqtt_port} · Topic：{settings.mqtt_topic}")
    ac_tab, washer_tab = st.tabs(["冷氣監測", "洗衣與 Main Agent"])
    with ac_tab:
        render_monitor(get_subscriber(settings))
    with washer_tab:
        from smart_home_energy.hems_ui import render_washer
        render_washer()


if __name__ == "__main__":
    main()

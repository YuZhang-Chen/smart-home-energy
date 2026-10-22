import { lazy, Suspense, useEffect, useState } from "react";
import {
  ArrowUpRight,
  Armchair,
  ChevronRight,
  CircleHelp,
  Lightbulb,
  Maximize,
  Fan,
  RotateCcw,
  SlidersHorizontal,
  Tv,
  WashingMachine,
  Wind,
  Blinds,
} from "lucide-react";
import { useCommands, useHome } from "./api";
import DayNightControls from "./DayNightControls";
import {
  calculateDayNight,
  clockWithMode,
  displayedMinute,
  localMinute,
  replaySimulation,
  setManualMinute,
  setPausedSimulationMinute,
  toggleSimulation,
  type DayNightState,
  type TimeMode,
} from "./dayNight";
import DevicePanel from "./DevicePanel";
import {
  devices,
  isFresh,
  labels,
  type DeviceId,
  type SceneDevice,
} from "./types";

const Scene = lazy(() => import("./Scene"));
const icons = {
  "ac-1": Wind,
  "washer-1": WashingMachine,
  "bulb-1": Lightbulb,
  "tv-1": Tv,
  "curtain-1": Blinds,
  "fan-1": Fan,
};

export default function App() {
  const [selected, setSelected] = useState<DeviceId>("ac-1");
  const [reset, setReset] = useState(0);
  const [showHelp, setShowHelp] = useState(false);
  const [dayNight, setDayNight] = useState<DayNightState>(() => ({
    mode: "real",
    manualMinute: localMinute(new Date()),
    anchorMinute: localMinute(new Date()),
    anchorTime: performance.now(),
    playing: false,
  }));
  const [, setClockTick] = useState(0);
  useEffect(() => {
    const refreshClock = () => setClockTick((value) => value + 1);
    const timer = window.setInterval(refreshClock, 1000);
    document.addEventListener("visibilitychange", refreshClock);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshClock);
    };
  }, []);
  const minute = displayedMinute(dayNight);
  const lighting = calculateDayNight(minute);
  const { home, error, refresh, realNow } = useHome();
  const { submit, pending, notice } = useCommands(refresh);
  const bulb = home?.devices["bulb-1"];
  const lightOn = bulb?.state?.power ?? false;
  const lightLevel = lightOn ? (bulb?.state?.level ?? 254) / 254 : 0;
  const lightMired = bulb?.state?.mired ?? 370;
  const device = devices.find((d) => d.id === selected)!;
  const connectedCount = home
    ? (["ac-1", "washer-1", "bulb-1"] as const).filter((id) =>
        isFresh(home.devices[id], realNow),
      ).length
    : 0;
  const stateText = (item: SceneDevice) => {
    if (item.id === "bulb-1")
      return !bulb?.state ? "等待回報" : !isFresh(bulb, realNow)
        ? "回報過期" : lightOn ? "燈光已開啟" : "燈光已關閉";
    if (item.id !== "ac-1" && item.id !== "washer-1") return "資訊展示";
    const snapshot = home?.devices[item.id];
    if (!snapshot?.state) return "等待連線";
    if (!isFresh(snapshot, realNow)) return "回報過期";
    return (
      labels[snapshot.active_task?.status ?? snapshot.state.status] ??
      "等待回報"
    );
  };
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="日常之間 首頁">
          <span className="brand-icon">
            <Armchair size={24} strokeWidth={1.6} />
          </span>
          <span>
            日常之間<small>SMART LIVING</small>
          </span>
        </a>
        <nav aria-label="主要導覽">
          <span className="nav-current">
            我的客廳
            <span />
          </span>
          <a
            href={home?.assistant_url ?? "http://127.0.0.1:8501"}
            target="_blank"
            rel="noreferrer"
          >
            家庭助理
            <ArrowUpRight size={13} />
          </a>
        </nav>
        <div className="local-status">
          <span
            className={`status-dot ${!error && connectedCount ? "" : "muted"}`}
          />
          本機智慧家庭
        </div>
      </header>
      <main>
        <section className="page-heading">
          <div>
            <div className="breadcrumb">
              我的家庭
              <ChevronRight size={12} />
              客廳
            </div>
            <h1>
              家的每一刻，<span>都在掌握。</span>
            </h1>
            <p>轉個角度，看看設備如何照顧你的日常。</p>
          </div>
          <div className="room-summary">
            <span>YOUR LIVING ROOM</span>
            <p>
              <b>06</b> 場景設備 <i /> <b>03</b> 控制設備
            </p>
          </div>
        </section>
        {error && (
          <div className="service-warning" role="status">
            {error}設備數值與動畫將依回報時間判定是否過期。
          </div>
        )}
        <div className="workspace">
          <div className="room-column">
            <section
              className="scene-card"
              aria-label="3D 智慧客廳"
              style={{
                background: `radial-gradient(ellipse at 50% 52%, ${lighting.sky} 0%, color-mix(in srgb, ${lighting.sky} 88%, white) 58%, color-mix(in srgb, ${lighting.sky} 78%, white) 100%)`,
              }}
            >
              <div className="scene-topbar">
                <div>
                  <span className="status-dot" />
                  <b>客廳</b>
                  <span className="scene-subtitle">LIVING ROOM / 01</span>
                </div>
                <span className="view-badge">45° 等角視圖</span>
              </div>
              <div className="scene-canvas">
                <Suspense
                  fallback={
                    <div className="scene-fallback">正在準備你的客廳…</div>
                  }
                >
                  <Scene
                    selected={selected}
                    onSelect={setSelected}
                    lightOn={lightOn}
                    lightLevel={lightLevel}
                    lightMired={lightMired}
                    dayNight={dayNight}
                    home={home}
                    realNow={realNow}
                    reset={reset}
                  />
                </Suspense>
              </div>
              <div className="scene-actions">
                <span className="scene-caption">
                  <span />
                  LOW-POLY HOME
                </span>
                <div>
                  <button
                    aria-label="操作說明"
                    aria-expanded={showHelp}
                    title="操作說明"
                    onClick={() => setShowHelp(!showHelp)}
                  >
                    <CircleHelp size={17} />
                  </button>
                  <button
                    aria-label="重設視角"
                    title="重設視角"
                    onClick={() => setReset((value) => value + 1)}
                  >
                    <RotateCcw size={17} />
                  </button>
                </div>
              </div>
              {showHelp && (
                <div className="scene-help" role="status">
                  <b>用你的角度，探索客廳</b>
                  <p>
                    按住拖曳：旋轉視角
                    <br />
                    滑動滾輪／雙指縮放：拉近拉遠
                    <br />
                    點擊家電或下方清單：查看設備
                    <br />
                    重設視角：回到 45° 俯視
                  </p>
                </div>
              )}
            </section>
            <div className="scene-bottom-note">
              <span>
                <Maximize size={13} />
                拖曳旋轉・滾輪縮放・點擊設備
              </span>
              <span>你的客廳，你的視角</span>
            </div>
            <DayNightControls
              mode={dayNight.mode}
              minute={minute}
              playing={dayNight.playing}
              onMode={(mode: TimeMode) =>
                setDayNight((current) =>
                  clockWithMode(current, mode, Date.now(), performance.now()),
                )
              }
              onMinute={(value) =>
                setDayNight((current) =>
                  current.mode === "simulation"
                    ? setPausedSimulationMinute(current, value)
                    : setManualMinute(current, value),
                )
              }
              onToggle={() =>
                setDayNight((current) => toggleSimulation(current, performance.now()))
              }
              onReplay={() =>
                setDayNight((current) => replaySimulation(current, performance.now()))
              }
            />
            <section className="device-list-section" aria-label="客廳設備清單">
              <div className="list-heading">
                <h2>
                  <SlidersHorizontal size={16} />
                  客廳設備 <span>6</span>
                </h2>
                <span>{connectedCount} / 3 台服務回報中</span>
              </div>
              <div className="device-list">
                {devices.map((item) => {
                  const Icon = icons[item.id];
                  return (
                    <button
                      key={item.id}
                      className={`device-chip ${selected === item.id ? "selected" : ""}`}
                      aria-pressed={selected === item.id}
                      onClick={() => setSelected(item.id)}
                    >
                      <Icon size={21} strokeWidth={1.6} />
                      <span>
                        <b>{item.name}</b>
                        <small>{stateText(item)}</small>
                      </span>
                      <ChevronRight size={14} />
                    </button>
                  );
                })}
              </div>
            </section>
            <div className="room-tip">
              <Lightbulb size={17} />
              <p>試著開關智慧燈泡，為客廳換一種氛圍。</p>
              <button onClick={() => setSelected("bulb-1")}>
                試試看
                <ArrowUpRight size={13} />
              </button>
            </div>
          </div>
          <div className="panel-column">
            {notice && (
              <div
                className={`request-notice ${notice.error ? "error" : ""}`}
                role={notice.error ? "alert" : "status"}
              >
                {notice.text}
              </div>
            )}
            <DevicePanel
              key={selected}
              device={device}
              home={home}
              realNow={realNow}
              lightOn={lightOn}
              setLightOn={(on) => submit("set_bulb", { device_id: "bulb-1", power: on })}
              submit={submit}
              busy={!!pending}
              connected={!!home && !error}
            />
          </div>
        </div>
      </main>
      <footer>
        <span>
          日常之間 <i /> 讓科技，安靜地融入生活。
        </span>
        <span>設備排程：Asia/Taipei <i /> 晝夜：本機／展示時間</span>
      </footer>
    </div>
  );
}

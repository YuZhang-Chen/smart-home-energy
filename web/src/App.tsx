import { lazy, memo, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { CopilotSidebar, useConfigureSuggestions, useRenderTool } from "@copilotkit/react-core/v2";
import { z } from "zod";
import { json, useCommands, useHome } from "./api";
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
import DevicePanel, { PlanForm } from "./DevicePanel";
import { DiagnosticsView, EnergyView, SettingsView } from "./SystemViews";
import {
  devices,
  isFresh,
  labels,
  type DeviceId,
  type SceneDevice,
  type Task,
  type Home,
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

// Keep the chat shell isolated from the room's frequent device-state updates.
const MemoizedCopilotSidebar = memo(CopilotSidebar);
const COPILOT_LABELS = {
  modalHeaderTitle: "家庭助理",
  chatInputPlaceholder: "描述你想安排的設備需求…",
};

export default function App() {
  const suggestionConfig = useMemo(
    () => ({
      consumerAgentId: "home",
      available: "before-first-message" as const,
      suggestions: [
        { title: "開冷氣", message: "幫我開冷氣一小時" },
        { title: "安排洗衣", message: "幫我安排洗衣" },
        { title: "調整燈光", message: "把客廳燈調到 40% 暖光" },
        { title: "打開電視", message: "幫我打開電視並切換到 HDMI 2" },
        { title: "拉開窗簾", message: "把客廳窗簾打開一半" },
      ],
    }),
    [],
  );
  useConfigureSuggestions(suggestionConfig);
  const [selected, setSelected] = useState<DeviceId>("ac-1");
  const [tvPower, setTvPower] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(true);
  const [page, setPage] = useState<"room" | "energy" | "settings" | "diagnostics">("room");
  const opener = useRef<HTMLElement | null>(null);
  const drawer = useRef<HTMLDivElement>(null);
  const [reset, setReset] = useState(0);
  const [showHelp, setShowHelp] = useState(false);
  const [dayNight, setDayNight] = useState<DayNightState>(() => ({
    mode: "real",
    manualMinute: localMinute(new Date()),
    anchorMinute: localMinute(new Date()),
    anchorTime: performance.now(),
    playing: false,
  }));
  // useHome already advances the displayed clock when its polling cycle completes.
  // A second root-level 1s timer forced the entire 3D room and page tree to render again.
  const minute = displayedMinute(dayNight);
  const lighting = calculateDayNight(minute);
  const { home, error, refresh, realNow } = useHome();
  const { submit, pending, notice } = useCommands(refresh);

  const curtainStatus = home?.devices["curtain-1"]?.state ?? null;
  useEffect(() => {
    const remoteTvPower = home?.devices["tv-1"]?.state?.power;
    if (typeof remoteTvPower === "boolean") {
      setTvPower(remoteTvPower);
    }
  }, [home?.devices["tv-1"]?.state?.power]);

  // The home snapshot is polled periodically. Do not re-register Copilot render
  // tools every time that snapshot changes: the chat UI can otherwise rebuild
  // its message renderer while the conversation is open.
  const renderContext = useRef<{
    home: Home | null;
    submit: (kind: string, payload: Record<string, unknown>) => void;
    busy: boolean;
  }>({ home, submit, busy: !!pending });
  renderContext.current = { home, submit, busy: !!pending };

  useRenderTool({
    name: "render_task_card",
    parameters: z.object({ task_id: z.string(), version: z.number(), device_id: z.string() }),
    render: ({ parameters }) => {
      const current = renderContext.current;
      return (
        <ProposalCard
          taskId={parameters.task_id}
          version={parameters.version}
          home={current.home}
          submit={current.submit}
          busy={current.busy}
        />
      );
    },
  }, []);
  useRenderTool({
    name: "render_light_card",
    parameters: z.object({ request_id: z.string() }),
    render: ({ parameters }) => parameters.request_id ? <LightActionCard requestId={parameters.request_id} /> : null,
  }, []);
  useRenderTool({
    name: "render_tv_card",
    parameters: z.object({ device_id: z.string().optional(), summary: z.string().optional() }),
    render: ({ parameters }) => (
      <TVActionCard
        summary={parameters.summary}
        home={renderContext.current.home}
      />
    ),
  }, []);
  useRenderTool({
    name: "render_curtain_card",
    parameters: z.object({ device_id: z.string().optional(), summary: z.string().optional() }),
    render: ({ parameters }) => (
      <CurtainActionCard
        summary={parameters.summary}
        home={renderContext.current.home}
      />
    ),
  }, []);
  useEffect(() => {
    if (!drawerOpen) return;
    const previous = opener.current ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setDrawerOpen(false); return; }
      if (event.key !== "Tab" || !drawer.current) return;
      const focusable = [...drawer.current.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(item => item.offsetParent !== null);
      if (!focusable.length) { event.preventDefault(); drawer.current.focus(); return; }
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    drawer.current?.querySelector<HTMLElement>("button, input, select, textarea")?.focus();
    return () => { document.removeEventListener("keydown", onKey); previous?.focus(); };
  }, [drawerOpen]);
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
    if (item.id === "curtain-1") {
      if (!curtainStatus) return "等待連線";

      const { position, target, status } = curtainStatus;

      if (status === "moving") {
        if (target > position + 0.5) return "開啟中";
        if (target < position - 0.5) return "關閉中";
      }

      if (position <= 1) return "全關";
      if (position >= 99) return "全開";

      return `半開 ${Math.round(position)}%`;
    }
    if (item.id === "tv-1") {
      const tv = home?.devices["tv-1"]?.state;
      if (!tv) return tvPower ? "運作中" : "待機中";
      if (!tv.power) return "待機中";
      const volText = tv.muted ? "靜音" : `${tv.volume}%`;
      const playText = tv.playback === "playing" ? " · 播放中" : tv.playback === "paused" ? " · 已暫停" : "";
      return `運作中 · ${tv.input} · ${volText}${playText}`;
    }
    if (item.id !== "ac-1" && item.id !== "washer-1") return "資訊展示";
    const snapshot = home?.devices[item.id];
    if (!snapshot?.state) return "等待連線";
    if (!isFresh(snapshot, realNow)) return "回報過期";
    return (
      labels[snapshot.active_task?.status ?? snapshot.state.status] ??
      "等待回報"
    );
  };
  const handleAssistantOpenChange = useCallback((open: boolean) => {
    setAssistantOpen(open);
    if (open) setDrawerOpen(false);
  }, []);
  const panelOpen = drawerOpen ? "left" : assistantOpen ? "right" : "none";
  return (
    <div className="app-shell" data-panel-open={panelOpen}>
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
          {([ ["room", "我的客廳"], ["energy", "用電紀錄"], ["settings", "偏好設定"], ["diagnostics", "系統診斷"] ] as const).map(([key, label]) => <button key={key} className={page === key ? "nav-current" : "nav-button"} aria-current={page === key ? "page" : undefined} onClick={() => setPage(key)}>{label}{page === key && <span />}</button>)}
        </nav>
        <div className="local-status">
          <span
            className={`status-dot ${!error && connectedCount ? "" : "muted"}`}
          />
          本機智慧家庭
        </div>
      </header>
      <main>
        {notice && <div className={`request-notice ${notice.error ? "error" : ""}`} role={notice.error ? "alert" : "status"}>{notice.text}</div>}
        {page === "energy" ? <EnergyView /> : page === "settings" ? <SettingsView submit={submit} busy={!!pending} /> : page === "diagnostics" ? <DiagnosticsView /> : <>
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
                    onSelect={(id) => { setSelected(id); setAssistantOpen(false); setDrawerOpen(true); }}
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
                      onClick={(event) => { opener.current = event.currentTarget; setSelected(item.id); setAssistantOpen(false); setDrawerOpen(true); }}
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
              <button onClick={(event) => { opener.current = event.currentTarget; setSelected("bulb-1"); setAssistantOpen(false); setDrawerOpen(true); }}>
                試試看
                <ArrowUpRight size={13} />
              </button>
            </div>
          </div>
        </div>
        {drawerOpen && <div className="drawer-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) setDrawerOpen(false); }}><div ref={drawer} className="device-drawer" role="dialog" aria-modal="true" aria-label={`${device.name}設備控制`}><div className="drawer-heading"><b>{device.name}設備控制</b><button onClick={() => setDrawerOpen(false)} aria-label="關閉設備控制">×</button></div><DevicePanel key={selected} device={device} home={home} realNow={realNow} lightOn={lightOn} setLightOn={(on) => submit("set_bulb", { device_id: "bulb-1", power: on })} tvPower={tvPower} onTVPowerChange={setTvPower} submit={submit} busy={!!pending} connected={!!home && !error} onRefresh={refresh} /></div></div>}
        </>}
      </main>
      <footer>
        <span>
          日常之間 <i /> 讓科技，安靜地融入生活。
        </span>
        <span>設備排程：Asia/Taipei <i /> 晝夜：本機／展示時間</span>
      </footer>
      <MemoizedCopilotSidebar
        agentId="home"
        threadId="home"
        open={assistantOpen}
        onOpenChange={handleAssistantOpenChange}
        width="25vw"
        labels={COPILOT_LABELS}
      />
    </div>
  );
}

export function LightActionCard({ requestId }: { requestId: string }) {
  const [data, setData] = useState<{status: string; result: string | null; action: {status: string} | null; target: {power?: boolean; brightness_percent?: number; kelvin?: number}} | null>(null);
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await json<NonNullable<typeof data>>(`/api/requests/${encodeURIComponent(requestId)}`);
        if (!stopped) { setData(result); setOffline(false); }
      } catch { if (!stopped) setOffline(true); }
      if (!stopped) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [requestId]);
  const status = data?.action?.status ?? (data?.status === "error" ? "rejected" : "queued");
  const labels: Record<string, string> = {queued: "等待處理", dispatched: "正在調整", accepted: "等待設備確認", confirmed: "設備已確認", rejected: "設備拒絕", unknown: "結果未知", superseded: "已被新指令取代", partial: "部分完成"};
  const target = data?.target;
  return <div className="proposal-card" aria-live="polite">
    <strong>客廳燈</strong><p>{offline ? "無法連線，正在重新查詢" : labels[status] ?? "處理中"}</p>
    {target && <p>{[target.power === undefined ? null : target.power ? "開啟" : "關閉", target.brightness_percent === undefined ? null : `亮度 ${target.brightness_percent}%`, target.kelvin === undefined ? null : `色溫 ${target.kelvin}K`].filter(Boolean).join(" · ")}</p>}
    {data?.result && <p>{data.result}</p>}
  </div>;
}

export function TVActionCard({
  summary,
  home,
}: {
  summary?: string;
  home: Home | null;
}) {
  const tv = home?.devices["tv-1"]?.state ?? null;
  const power = tv?.power ?? false;
  const muted = tv?.muted ?? false;
  const playLabel = tv?.playback === "playing" ? "播放中" : tv?.playback === "paused" ? "已暫停" : "已停止";
  return (
    <div className="proposal-card" aria-live="polite">
      <strong>客廳電視</strong>
      <p>
        {tv
          ? power
            ? `運作中 · ${tv.input} · ${muted ? "靜音" : `音量 ${tv.volume}%`} · ${playLabel}`
            : "待機中"
          : "已套用設定"}
      </p>
      {summary && <p>{summary}</p>}
    </div>
  );
}

export function CurtainActionCard({
  summary,
  home,
}: {
  summary?: string;
  home: Home | null;
}) {
  const snap = home?.devices["curtain-1"];
  const curtain = snap?.state ?? null;
  const schedulesCount = snap?.schedules?.filter((s) => s.enabled).length ?? 0;
  return (
    <div className="proposal-card" aria-live="polite">
      <strong>客廳窗簾</strong>
      <p>
        {curtain
          ? `目前 ${Math.round(curtain.position)}%（目標 ${Math.round(curtain.target)}%）${schedulesCount > 0 ? ` · 啟用排程 ${schedulesCount} 筆` : ""}`
          : "已套用設定"}
      </p>
      {summary && <p>{summary}</p>}
    </div>
  );
}

export function ProposalCard({ taskId, version, home, submit, busy }: { taskId?: string; version?: number; home: Home | null; submit: (kind: string, payload: Record<string, unknown>) => void; busy: boolean }) {
  const [task, setTask] = useState<Task | null>(null);
  const [reviewedVersion, setReviewedVersion] = useState(version);
  const [editing, setEditing] = useState(false);
  const [details, setDetails] = useState("");
  const [maxCost, setMaxCost] = useState<number | null>(null);
  useEffect(() => { if (!taskId) return; let live = true; const read = () => fetch(`/api/tasks/${encodeURIComponent(taskId)}`).then(r => r.ok ? r.json() : null).then(v => { if (live) setTask(v); }).catch(() => {}); void read(); const timer = window.setInterval(read, 2500); return () => { live = false; clearInterval(timer); }; }, [taskId]);
  if (!taskId || !task) return <div className="proposal-card">正在讀取已保存的設備計畫…</div>;
  const current = task.version === reviewedVersion, plan = task.ac_plan ?? task.plan, approvable = current && task.status === "pending" && plan;
  const payload = { task_id: task.task_id, expected_version: task.version };
  const editable = current && ["needs_details", "pending", "scheduled"].includes(task.status);
  const abortable = current && ["starting", "running", "stopping", "unknown"].includes(task.status);
  const resolvable = current && ["failed", "unknown"].includes(task.status);
  const formatDate = (value: string) => new Intl.DateTimeFormat("zh-TW", { timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
  const time = (value: number | null) => value === null ? "無" : `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  const device = devices.find(item => item.id === task.device_id);
  return <article className="proposal-card">
    <b>{task.device_id === "ac-1" ? "冷氣計畫" : "洗衣計畫"}</b>
    <span>{labels[task.status] ?? task.status} · 版本 {task.version}</span>
    {!current && <><span>計畫已更新，請檢視最新版本。</span><button onClick={() => { setReviewedVersion(task.version); setEditing(false); setMaxCost(null); }}>載入最新版本</button></>}
    {plan && <><small>{formatDate(task.ac_plan?.constraints.start ?? task.plan!.start)} – {formatDate(task.ac_plan?.constraints.end ?? task.plan!.end)}（台北）</small>
      {task.ac_plan && <small>目標 {task.ac_plan.constraints.target_c}°C，舒適範圍 {task.ac_plan.constraints.minimum_c}–{task.ac_plan.constraints.maximum_c}°C</small>}
      {task.constraints && <small>最早 {formatDate(task.constraints.earliest)}；期限 {formatDate(task.constraints.deadline)}；安靜時段 {time(task.constraints.quiet_start)}–{time(task.constraints.quiet_end)}；行程 {task.plan?.program.name}</small>}
      <small>{plan.tariff.official ? "台電費率增量估算" : "模擬費率估算"} · {plan.energy_kwh.toFixed(3)} kWh · NT$ {plan.cost.toFixed(2)}</small></>}
    {approvable && <>{task.device_id === "washer-1" && <label>核准費用上限（NT$）<input type="number" min={plan.cost} step="any" value={maxCost ?? plan.cost} onChange={event => setMaxCost(event.target.valueAsNumber)} /></label>}
      <button disabled={busy || (task.device_id === "washer-1" && maxCost !== null && (!Number.isFinite(maxCost) || maxCost < plan.cost))} onClick={() => submit("approve", { ...payload, ...(task.device_id === "washer-1" ? { max_cost: maxCost ?? plan.cost } : {}) })}>確認計畫</button></>}
    {editable && <div className="proposal-actions"><button disabled={busy} onClick={() => setEditing(!editing)}>修改計畫</button><button className="secondary-action" disabled={busy} onClick={() => submit("cancel", payload)}>取消計畫</button></div>}
    {editing && editable && home && device && <><PlanForm key={`${task.task_id}:${task.version}`} device={device} home={home} task={task} submit={submit} busy={busy} /><label>自然語言補充<textarea value={details} onChange={event => setDetails(event.target.value)} /></label><button disabled={busy || !details.trim()} onClick={() => { submit("details", { ...payload, text: details.trim() }); setDetails(""); }}>送出補充需求</button></>}
    {abortable && <button className="secondary-action" disabled={busy} onClick={() => { if (window.confirm("送出中止請求？設備回報確認前，任務仍會顯示處理中。")) submit("abort", { ...payload, confirmed: true }); }}>中止設備任務</button>}
    {resolvable && <button className="secondary-action" disabled={busy} onClick={() => { if (window.confirm("已確認設備安全並要解除異常？")) submit("reset", { ...payload, confirmed: true }); }}>確認狀態並解除異常</button>}
  </article>;
}

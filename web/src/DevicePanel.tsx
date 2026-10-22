import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowRight,
  CalendarClock,
  Check,
  ChevronDown,
  CircleHelp,
  Fan,
  MapPin,
  Power,
  Radio,
  ShieldCheck,
  TriangleAlert,
  Waves,
  Wind,
} from "lucide-react";
import {
  dateLabel,
  isFresh,
  labels,
  localInput,
  toISO,
  type DeviceSnapshot,
  type Home,
  type SceneDevice,
  type Task,
} from "./types";

type Submit = (kind: string, payload: Record<string, unknown>) => void;
const addMinutes = (iso: string, minutes: number) =>
  new Date(Date.parse(iso) + minutes * 60_000).toISOString();
const timeInput = (minutes: number) =>
  `${Math.floor(minutes / 60)
    .toString()
    .padStart(2, "0")}:${(minutes % 60).toString().padStart(2, "0")}`;
const minutes = (value: string) =>
  Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));
const finite = (value: number | undefined, digits = 0) =>
  value === undefined ? "—" : value.toFixed(digits);

export function PlanForm({
  device,
  home,
  task,
  submit,
  busy,
}: {
  device: SceneDevice;
  home: Home;
  task: Task | null;
  submit: Submit;
  busy: boolean;
}) {
  const ac = device.id === "ac-1",
    prefs = home.preferences;
  const c = task?.ac_constraints,
    w = task?.constraints;
  const [start, setStart] = useState(
    localInput(
      ac
        ? (c?.start ?? addMinutes(home.clock.now, 10))
        : (w?.earliest ?? addMinutes(home.clock.now, 5)),
    ),
  );
  const [end, setEnd] = useState(
    localInput(
      ac
        ? (c?.end ?? addMinutes(home.clock.now, 10 + prefs.duration_minutes))
        : (w?.deadline ?? addMinutes(home.clock.now, prefs.washer_hours * 60)),
    ),
  );
  const [target, setTarget] = useState(c?.target_c ?? prefs.target_c);
  const [minimum, setMinimum] = useState(c?.minimum_c ?? prefs.minimum_c);
  const [maximum, setMaximum] = useState(c?.maximum_c ?? prefs.maximum_c);
  const [quiet, setQuiet] = useState(w ? w.quiet_start !== null : true);
  const [quietStart, setQuietStart] = useState(
    timeInput(w?.quiet_start ?? prefs.quiet_start),
  );
  const [quietEnd, setQuietEnd] = useState(
    timeInput(w?.quiet_end ?? prefs.quiet_end),
  );
  const [error, setError] = useState("");
  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    setError("");
    if (Date.parse(toISO(end)) <= Date.parse(toISO(start))) {
      setError("結束時間必須晚於開始時間。");
      return;
    }
    if (
      ac &&
      !(
        16 <= minimum &&
        minimum <= target &&
        target <= maximum &&
        maximum <= 30 &&
        minimum < maximum
      )
    ) {
      setError("目標溫度須在舒適範圍內，且上下限不能相同。");
      return;
    }
    if (!ac && quiet && quietStart === quietEnd) {
      setError("安靜時段的開始與結束不能相同。");
      return;
    }
    const context = task
      ? { task_id: task.task_id, expected_version: task.version }
      : {};
    const constraints = ac
      ? {
          start: toISO(start),
          end: toISO(end),
          target_c: target,
          minimum_c: minimum,
          maximum_c: maximum,
          ambient_c: c?.ambient_c ?? 32,
          warming_seconds: c?.warming_seconds ?? 1800,
          device_id: "ac-1",
        }
      : {
          earliest: toISO(start),
          deadline: toISO(end),
          quiet_start: quiet ? minutes(quietStart) : null,
          quiet_end: quiet ? minutes(quietEnd) : null,
          device_id: "washer-1",
        };
    submit(
      ac
        ? task
          ? "schedule_ac_form"
          : "create_ac_form"
        : task
          ? "schedule_form"
          : "create_form",
      { device_id: device.id, constraints, ...context },
    );
  };
  return (
    <form className="plan-form" onSubmit={handleSubmit}>
      <div className="section-title">
        <CalendarClock size={17} />
        <h3>{task ? "修改計畫" : "安排新的計畫"}</h3>
      </div>
      <p className="form-hint">
        {ac
          ? "設定舒適時段，確認計畫後才會開啟。"
          : "我們會在允許時段內安排完整洗衣行程。"}
      </p>
      <fieldset disabled={busy}>
        <label>
          {ac ? "開始時間" : "最早開始"}
          <input
            type="datetime-local"
            required
            aria-label={ac ? "開始時間" : "最早開始"}
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          {ac ? "結束時間" : "最晚完成"}
          <input
            type="datetime-local"
            required
            aria-label={ac ? "結束時間" : "最晚完成"}
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
        {ac ? (
          <>
            <label>
              目標溫度 <span className="field-unit">°C</span>
              <input
                type="number"
                min="16"
                max="30"
                step="0.5"
                required
                value={target}
                onChange={(e) => setTarget(e.target.valueAsNumber)}
              />
            </label>
            <div className="field-pair">
              <label>
                舒適下限 °C
                <input
                  type="number"
                  min="16"
                  max="30"
                  step="0.5"
                  required
                  value={minimum}
                  onChange={(e) => setMinimum(e.target.valueAsNumber)}
                />
              </label>
              <label>
                舒適上限 °C
                <input
                  type="number"
                  min="16"
                  max="30"
                  step="0.5"
                  required
                  value={maximum}
                  onChange={(e) => setMaximum(e.target.valueAsNumber)}
                />
              </label>
            </div>
          </>
        ) : (
          <>
            <div className="program-info">
              <Waves size={18} />
              <span>
                標準洗衣{" "}
                <b>
                  {home.program.stages.reduce(
                    (sum, stage) => sum + stage.seconds,
                    0,
                  ) / 60}{" "}
                  分鐘
                </b>
              </span>
            </div>
            <label className="check-label">
              <input
                type="checkbox"
                checked={quiet}
                onChange={(e) => setQuiet(e.target.checked)}
              />
              避開安靜時段
            </label>
            {quiet && (
              <div className="field-pair">
                <label>
                  安靜時段開始
                  <input
                    type="time"
                    required
                    value={quietStart}
                    onChange={(e) => setQuietStart(e.target.value)}
                  />
                </label>
                <label>
                  安靜時段結束
                  <input
                    type="time"
                    required
                    value={quietEnd}
                    onChange={(e) => setQuietEnd(e.target.value)}
                  />
                </label>
              </div>
            )}
          </>
        )}
        {error && (
          <p role="alert" className="inline-error">
            {error}
          </p>
        )}
        <button className="button primary wide" type="submit">
          {busy ? "請求處理中…" : "計算並提出計畫"}
          <ArrowRight size={16} />
        </button>
      </fieldset>
      <p className="timezone-note">
        Asia/Taipei ·{" "}
        {home.clock.speed === 1 ? "真實時間" : `${home.clock.speed} 倍展示時間`}
      </p>
    </form>
  );
}

function TaskActions({
  task,
  snapshot,
  realNow,
  submit,
  busy,
}: {
  task: Task;
  snapshot: DeviceSnapshot;
  realNow: number;
  submit: Submit;
  busy: boolean;
}) {
  const [confirmStop, setConfirmStop] = useState(false);
  const [checked, setChecked] = useState(false);
  const plan = task.ac_plan ?? task.plan;
  const [maxCost, setMaxCost] = useState(plan?.cost ?? 0);
  const context = { task_id: task.task_id, expected_version: task.version };
  const state = snapshot.state;
  const canStop =
    isFresh(snapshot, realNow) &&
    state?.status === "running" &&
    state.task_id === task.task_id &&
    !!state.run_id &&
    state.run_id === task.run_id;
  return (
    <div className="task-actions">
      {task.status === "pending" && plan && (
        <>
          {task.device_id === "washer-1" && (
            <label>
              核准費用上限（NT$）
              <input
                aria-label="核准費用上限"
                type="number"
                min={plan.cost}
                step="any"
                required
                value={maxCost}
                disabled={busy}
                onChange={(e) => setMaxCost(e.target.valueAsNumber)}
              />
            </label>
          )}
          <button
            className="button primary wide"
            disabled={
              busy ||
              (task.device_id === "washer-1" &&
                (!Number.isFinite(maxCost) || maxCost < plan.cost))
            }
            onClick={() =>
              submit("approve", {
                ...context,
                ...(task.device_id === "washer-1" ? { max_cost: maxCost } : {}),
              })
            }
          >
            <Check size={16} />
            {task.device_id === "ac-1" ? "核准冷氣計畫" : "核准洗衣計畫"}
          </button>
        </>
      )}
      {["needs_details", "pending", "scheduled"].includes(task.status) && (
        <button
          className="button subtle wide"
          disabled={busy}
          onClick={() => submit("cancel", context)}
        >
          取消計畫
        </button>
      )}
      {["starting", "running", "unknown"].includes(task.status) &&
        (confirmStop ? (
          <div className="confirm-box">
            <p>確定現在停止嗎？這次任務不會自動重跑。</p>
            <div className="field-pair">
              <button
                className="button danger"
                disabled={busy || !canStop}
                onClick={() => submit("abort", { ...context, confirmed: true })}
              >
                確認停止
              </button>
              <button
                className="button subtle"
                onClick={() => setConfirmStop(false)}
              >
                繼續運轉
              </button>
            </div>
          </div>
        ) : (
          <button
            className="button subtle wide"
            disabled={busy || !canStop}
            onClick={() => setConfirmStop(true)}
          >
            <Power size={16} />
            {task.device_id === "ac-1" ? "關閉冷氣" : "停止洗衣"}
          </button>
        ))}
      {["failed", "unknown"].includes(task.status) && (
        <>
          <button
            className="button subtle wide"
            disabled={busy}
            onClick={() => submit("query", context)}
          >
            重新查核設備
          </button>
          <label className="check-label">
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => setChecked(e.target.checked)}
            />
            我已檢查設備，確認可以解除
          </label>
          <button
            className="button subtle wide"
            disabled={busy || !checked || !isFresh(snapshot, realNow)}
            onClick={() => submit("reset", { ...context, confirmed: true })}
          >
            解除異常
          </button>
        </>
      )}
    </div>
  );
}

function TaskCard({
  task,
  snapshot,
  realNow,
  submit,
  busy,
  active,
}: {
  task: Task;
  snapshot: DeviceSnapshot;
  realNow: number;
  submit: Submit;
  busy: boolean;
  active: boolean;
}) {
  const plan = task.ac_plan ?? task.plan;
  const matching =
    snapshot.state?.task_id === task.task_id &&
    !!task.run_id &&
    snapshot.state?.run_id === task.run_id;
  const state = matching ? snapshot.state : null;
  return (
    <section className="task-card" aria-label="設備計畫">
      <div className="task-heading">
        <span className="eyebrow">{active ? "CURRENT PLAN" : "LAST PLAN"}</span>
        <span
          className={`status-tag ${task.status === "running" ? "active" : ""}`}
        >
          {labels[task.status] ?? task.status}
        </span>
      </div>
      {plan && (
        <>
          <p className="plan-dates">
            {dateLabel(
              task.ac_plan ? task.ac_plan.constraints.start : task.plan!.start,
            )}
            <ArrowRight size={13} />
            {dateLabel(
              task.ac_plan ? task.ac_plan.constraints.end : task.plan!.end,
            )}
          </p>
          {task.ac_plan && (
            <p className="plan-detail">
              目標 {task.ac_plan.constraints.target_c}°C · 舒適{" "}
              {task.ac_plan.constraints.minimum_c}–
              {task.ac_plan.constraints.maximum_c}°C
            </p>
          )}
          <div className="quote-grid">
            <div>
              <span>預估用電</span>
              <strong>
                {plan.energy_kwh.toFixed(3)}
                <small> kWh</small>
              </strong>
            </div>
            <div>
              <span>預估費用</span>
              <strong>
                <small>NT$ </small>
                {plan.cost.toFixed(2)}
              </strong>
            </div>
          </div>
          <p className="fine-print">
            {plan.tariff.official ? "台電費率增量估算" : "模擬費率估算"}
            ，非實際帳單。{task.ac_plan && "費用超估會提醒，仍以舒適優先。"}
          </p>
        </>
      )}
      {state && (
        <div className="progress-area">
          <div>
            <span>{task.device_id === "ac-1" ? "時段進度" : state.stage}</span>
            <b>{Math.round(state.progress * 100)}%</b>
          </div>
          <progress aria-label="任務進度" max={1} value={state.progress} />
        </div>
      )}
      {task.message && <p className="task-message">{task.message}</p>}
      {active && (
        <TaskActions
          key={`${task.task_id}:${task.version}`}
          task={task}
          snapshot={snapshot}
          realNow={realNow}
          submit={submit}
          busy={busy}
        />
      )}
    </section>
  );
}

export default function DevicePanel({
  device,
  home,
  realNow,
  lightOn,
  setLightOn,
  submit,
  busy,
  connected,
}: {
  device: SceneDevice;
  home: Home | null;
  realNow: number;
  lightOn: boolean;
  setLightOn: (on: boolean) => void;
  submit: Submit;
  busy: boolean;
  connected: boolean;
}) {
  const live =
    device.id === "ac-1" || device.id === "washer-1" ? device.id : null;
  const snapshot = live ? home?.devices[live] : undefined;
  const state = snapshot?.state,
    fresh = isFresh(snapshot, realNow);
  const task = snapshot?.active_task ?? snapshot?.latest_task;
  const [showEdit, setShowEdit] = useState(false);
  const bulbState = home?.devices["bulb-1"]?.state;
  const bulbFresh = isFresh(home?.devices["bulb-1"], realNow);
  const [brightness, setBrightness] = useState(100);
  const [kelvin, setKelvin] = useState(2700);
  useEffect(() => {
    if (bulbState) {
      setBrightness(Math.max(1, Math.round(bulbState.level * 100 / 254)));
      setKelvin(Math.round(1_000_000 / bulbState.mired / 100) * 100);
    }
  }, [bulbState?.state_version]);
  const canEdit =
    !snapshot?.active_task ||
    ["needs_details", "pending", "scheduled"].includes(
      snapshot.active_task.status,
    );
  return (
    <aside className="device-panel" aria-label="設備控制面板">
      <div className="panel-topline">
        <span className="eyebrow">DEVICE DETAILS</span>
        <span className="panel-number">
          {live || device.id === "bulb-1"
            ? "已串接虛擬設備"
            : device.source === "local"
              ? "本頁互動"
              : "場景設備"}
        </span>
      </div>
      <div className="device-title">
        <div className="device-icon-large">
          {device.id === "ac-1" ? (
            <Wind size={30} strokeWidth={1.4} />
          ) : device.id === "washer-1" ? (
            <Waves size={30} strokeWidth={1.4} />
          ) : device.id === "fan-1" ? (
            <Fan size={30} strokeWidth={1.4} />
          ) : (
            <Radio size={28} strokeWidth={1.4} />
          )}
        </div>
        <div>
          <h2>{device.name}</h2>
          <p>{device.english}</p>
        </div>
      </div>
      <p className="location">
        <MapPin size={13} />
        {device.location}
      </p>
      {live ? (
        <>
          <div
            className={`connection-strip ${fresh && connected ? "online" : "waiting"}`}
          >
            <span className="status-dot" />
            {!state
              ? "等待設備首次回報"
              : !fresh
                ? "設備回報已過期，保留最後數值"
                : !connected
                  ? "連線中斷，正在重新連線"
                  : (labels[state.status] ?? state.status)}
          </div>
          <div className="telemetry-grid">
            <div className="metric main-metric">
              <span>{live === "ac-1" ? "目前室溫" : "洗衣進度"}</span>
              <strong>
                {live === "ac-1"
                  ? finite(state?.temperature_c, 1)
                  : state
                    ? Math.round(state.progress * 100)
                    : "—"}
                <small>{live === "ac-1" ? "°C" : "%"}</small>
              </strong>
            </div>
            <div className="metric">
              <span>即時功率</span>
              <strong>
                {finite(state?.power_w)}
                <small> W</small>
              </strong>
            </div>
            <div className="metric">
              <span>本任務用電</span>
              <strong>
                {finite(state?.energy_kwh, 3)}
                <small> kWh</small>
              </strong>
            </div>
          </div>
          {state && (
            <p className="last-seen">
              最後回報 {dateLabel(state.observed_at)}（台北）
            </p>
          )}
          {task && snapshot && (
            <TaskCard
              task={task}
              snapshot={snapshot}
              realNow={realNow}
              submit={submit}
              busy={busy || !connected}
              active={!!snapshot.active_task}
            />
          )}
          {home && canEdit && (
            <>
              {snapshot?.active_task && (
                <button
                  className="edit-toggle"
                  aria-expanded={showEdit}
                  onClick={() => setShowEdit(!showEdit)}
                >
                  調整計畫條件
                  <ChevronDown size={16} />
                </button>
              )}
              {(!snapshot?.active_task || showEdit) && (
                <PlanForm
                  key={snapshot?.active_task?.task_id ?? `${device.id}:new`}
                  device={device}
                  home={home}
                  task={snapshot?.active_task ?? null}
                  submit={submit}
                  busy={busy || !connected}
                />
              )}
            </>
          )}
          {!home && (
            <div className="empty-note">
              <TriangleAlert size={19} />
              <p>請啟動本機控制服務。連線恢復後，設備資訊會自動出現。</p>
            </div>
          )}
          <div className="panel-footnote">
            <ShieldCheck size={16} />
            <p>
              計畫經你核准才會執行。關閉網頁後，已核准任務仍由本機服務管理。
            </p>
          </div>
        </>
      ) : device.id === "bulb-1" ? (
        <>
          <div className={`connection-strip ${bulbFresh && connected ? "online" : "waiting"}`}>
            <span className="status-dot" />
            {!bulbState ? "等待設備首次回報" : !bulbFresh || !connected
              ? "設備回報已過期，保留最後數值" : bulbState.transitioning
                ? "燈光調整中" : bulbState.fault ?? "設備狀態已同步"}
          </div>
          <div className="light-preview" data-on={lightOn}>
            <div className="light-orb" />
            <span>
              {lightOn ? "讓日常，多一點溫暖。" : "留一點安靜，給此刻。"}
            </span>
          </div>
          <div className="light-control">
            <div>
              <h3>客廳照明</h3>
              <p>{lightOn ? `已開啟 · ${Math.round((bulbState?.level ?? 254) * 100 / 254)}% · ${Math.round(1_000_000 / (bulbState?.mired ?? 370))}K` : "已關閉 · 保留環境光"}</p>
            </div>
            <button
              className="switch"
              role="switch"
              aria-label="客廳燈泡"
              aria-checked={lightOn}
              disabled={busy || !connected || !bulbFresh}
              onClick={() => setLightOn(!lightOn)}
            >
              <span />
            </button>
          </div>
          <div className="light-settings">
            <label>亮度 {brightness}%
              <input type="range" aria-label="燈泡亮度" min="1" max="100" value={brightness}
                disabled={busy || !connected || !bulbFresh}
                onChange={(event) => setBrightness(Number(event.target.value))} />
            </label>
            <label>色溫 K
              <input type="number" aria-label="燈泡色溫" min="2700" max="6500" step="100" value={kelvin}
                disabled={busy || !connected || !bulbFresh}
                onChange={(event) => setKelvin(Number(event.target.value))} />
            </label>
            <button type="button" disabled={busy || !connected || !bulbFresh || kelvin < 2700 || kelvin > 6500}
              onClick={() => submit("set_bulb", { device_id: "bulb-1", power: true,
                brightness_percent: brightness, kelvin })}>
              套用照明設定
            </button>
          </div>
          <div className="panel-footnote">
            <CircleHelp size={16} />
            <p>照明效果依設備回報同步；關閉時保留上次亮度與色溫。</p>
          </div>
        </>
      ) : (
        <div className="display-info">
          <Radio size={30} strokeWidth={1.3} />
          <h3>留給未來的智慧日常</h3>
          <p>
            這是客廳中的{device.name}
            。目前提供場景與位置資訊，尚未串接設備服務。
          </p>
          <span className="status-tag">資訊展示</span>
        </div>
      )}
    </aside>
  );
}

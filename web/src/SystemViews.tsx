import { useEffect, useMemo, useState } from "react";
import { json, useLighting } from "./api";
import { dateLabel } from "./types";

type Props = { submit: (kind: string, payload: Record<string, unknown>) => void; busy: boolean };
type Settings = { preferences: Record<string, number>; tariff: { plan: string; baseline_kwh: Record<string, number> }; tariff_plans: Record<string, string>; tariff_source: Record<string, string>; clock: { speed: number }; active_tasks: unknown[] };

const minutesToTime = (total: number) =>
  `${Math.floor(total / 60).toString().padStart(2, "0")}:${(total % 60).toString().padStart(2, "0")}`;
const timeToMinutes = (value: string) =>
  Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));
const timeOnlyLabel = (iso: string) =>
  new Intl.DateTimeFormat("zh-TW", { timeZone: "Asia/Taipei", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(iso));

function lightingReasonLabel(reason: string, overrideUntil: string | null, lastError: string | null): string {
  if (lastError) return `套用失敗（${lastError}）`;
  if (reason === "disabled") return "停用";
  if (reason === "day") return "白天關燈";
  if (reason === "night") return "晚間照明";
  if (reason === "manual_override") {
    return overrideUntil ? `手動暫停至 ${timeOnlyLabel(overrideUntil)}` : "手動暫停中";
  }
  return reason;
}

function useResource<T>(path: string) {
  const [value, setValue] = useState<T | null>(null), [error, setError] = useState("");
  useEffect(() => { let active = true; const load = () => json<T>(path).then(v => { if (active) { setValue(v); setError(""); } }).catch(() => { if (active) setError("無法讀取本機資料，請確認服務已啟動。"); }); void load(); const timer = window.setInterval(load, 5000); return () => { active = false; clearInterval(timer); }; }, [path]);
  return { value, error };
}

export function EnergyView() {
  const { value, error } = useResource<{ incremental_cost: number; tasks: { task_id: string; device_id: string; status: string; plan?: { energy_kwh: number; cost: number } | null; ac_plan?: { energy_kwh: number; cost: number } | null }[]; samples: { device_id: string; observed_at: string; power_w?: number }[] }>("/api/energy");
  const points = useMemo(() => { const samples = value?.samples ?? []; const max = Math.max(1, ...samples.map(s => Number(s.power_w ?? 0))); return samples.slice(-180).map((s, i, arr) => `${arr.length < 2 ? 0 : (i / (arr.length - 1)) * 100},${100 - Number(s.power_w ?? 0) / max * 88}`).join(" "); }, [value]);
  return <section className="system-view"><div className="breadcrumb">家庭管理　›　用電紀錄</div><h1>用電紀錄</h1><p className="system-intro">查看設備回報功率、排程估算及已核准任務的增量費用。</p>{error && <div className="service-warning">{error}</div>}<div className="system-cards"><article className="system-card energy-total"><span>家庭任務增量費用</span><strong>NT$ {Number(value?.incremental_cost ?? 0).toFixed(2)}</strong><small>依已核准設備任務估算</small></article><article className="system-card"><span>功率趨勢</span><div className="chart-wrap"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="最近功率樣本趨勢"><polyline points={points} fill="none" stroke="#668f75" strokeWidth="1.7" vectorEffect="non-scaling-stroke" /></svg></div><small>{value?.samples.length ?? 0} 筆回報樣本（最多 900 筆）</small></article></div><h2>近期設備任務</h2><div className="system-table"><div className="table-head"><span>設備</span><span>狀態</span><span>預估用電</span><span>預估費用</span></div>{(value?.tasks ?? []).slice(0, 20).map(t => { const p = t.ac_plan ?? t.plan; return <div className="table-row" key={t.task_id}><span>{t.device_id === "ac-1" ? "冷氣" : "洗衣機"}</span><span>{t.status}</span><span>{p ? `${p.energy_kwh.toFixed(3)} kWh` : "—"}</span><span>{p ? `NT$ ${p.cost.toFixed(2)}` : "—"}</span></div>; })}</div></section>;
}

export function SettingsView({ submit, busy }: Props) {
  const { value, error } = useResource<Settings>("/api/settings");
  const { lighting, error: lightingError } = useLighting();
  const [prefs, setPrefs] = useState<Record<string, number>>({}), [plan, setPlan] = useState("simulated"), [baseline, setBaseline] = useState<Record<string, string>>({}), [baselineMonth, setBaselineMonth] = useState(() => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei", year: "numeric", month: "2-digit" }).format(new Date())), [baselineValue, setBaselineValue] = useState(""), [speed, setSpeed] = useState(1);
  const [initialized, setInitialized] = useState(false);
  const [lightEnabled, setLightEnabled] = useState(false);
  const [dayStart, setDayStart] = useState("06:00");
  const [nightStart, setNightStart] = useState("18:00");
  const [lightBrightness, setLightBrightness] = useState(40);
  const [lightKelvin, setLightKelvin] = useState(2700);
  const [lightVersion, setLightVersion] = useState<number | null>(null);
  useEffect(() => { if (!value || initialized) return; setPrefs(value.preferences); setPlan(value.tariff.plan); setBaseline(Object.fromEntries(Object.entries(value.tariff.baseline_kwh).map(([month, amount]) => [month, String(amount)]))); setSpeed(value.clock.speed); setInitialized(true); }, [value, initialized]);
  useEffect(() => {
    if (!lighting || lighting.rule.version === lightVersion) return;
    setLightEnabled(lighting.rule.enabled);
    setDayStart(minutesToTime(lighting.rule.day_start));
    setNightStart(minutesToTime(lighting.rule.night_start));
    setLightBrightness(lighting.rule.brightness);
    setLightKelvin(lighting.rule.kelvin);
    setLightVersion(lighting.rule.version);
  }, [lighting, lightVersion]);
  const pref = (key: string, label: string, min: number, max: number, step = 1) => <label className="setting-field" key={key}>{label}<input type="number" min={min} max={max} step={step} value={prefs[key] ?? ""} onChange={e => setPrefs(current => ({ ...current, [key]: Number(e.target.value) }))} /></label>;
  const lightingInvalid = dayStart === nightStart;
  return <section className="system-view"><div className="breadcrumb">家庭管理　›　偏好與進階設定</div><h1>偏好與設定</h1><p className="system-intro">調整助理採用的預設條件與費率資料。設定更新會交由本機控制服務保存。</p>{error && <div className="service-warning">{error}</div>}{value && <><article className="system-card settings-card"><h2>自動照明</h2><p>依時段自動控制客廳燈泡（bulb-1）：白天自動關燈，晚間套用指定亮度與色溫。手動操作燈泡會暫停自動規則至下一個時段邊界。</p>{lightingError && <div className="service-warning">{lightingError}</div>}{lighting && <><p className="fine-print">目前時段：{lighting.status.window === "day" ? "白天" : "晚間"} · 狀態：{lightingReasonLabel(lighting.status.reason, lighting.status.override_until, lighting.status.last_error)}{lighting.status.next_boundary ? ` · 下一次切換：${dateLabel(lighting.status.next_boundary)}（台北）` : ""}</p><label className="check-label"><input type="checkbox" aria-label="啟用自動照明" checked={lightEnabled} onChange={e => setLightEnabled(e.target.checked)} />啟用日夜自動照明</label><div className="settings-grid"><label className="setting-field">白天開始時間<input type="time" aria-label="白天開始時間" value={dayStart} onChange={e => setDayStart(e.target.value)} /></label><label className="setting-field">晚間開始時間<input type="time" aria-label="晚間開始時間" value={nightStart} onChange={e => setNightStart(e.target.value)} /></label><label className="setting-field">晚間亮度（{lightBrightness}%）<input type="range" aria-label="自動照明亮度" min="1" max="100" value={lightBrightness} onChange={e => setLightBrightness(Number(e.target.value))} /></label><label className="setting-field">晚間色溫（{lightKelvin}K）<input type="range" aria-label="自動照明色溫" min="2700" max="6500" step="100" value={lightKelvin} onChange={e => setLightKelvin(Number(e.target.value))} /></label></div>{lightingInvalid && <p className="service-warning">白天與晚間開始時間不能相同。</p>}<button className="primary-action" disabled={busy || lightingInvalid} onClick={() => submit("lighting_rule", { device_id: "bulb-1", expected_version: lighting.rule.version, enabled: lightEnabled, day_start: timeToMinutes(dayStart), night_start: timeToMinutes(nightStart), brightness: lightBrightness, kelvin: lightKelvin, presence_enabled: false })}>儲存自動照明</button>{lighting.status.reason === "manual_override" && <button type="button" className="secondary-action" disabled={busy} onClick={() => submit("lighting_resume", { device_id: "bulb-1" })}>恢復自動</button>}</>}</article><article className="system-card settings-card"><h2>助理常用偏好</h2><div className="settings-grid">{pref("target_c", "冷氣目標溫度（°C）", 16, 30, .5)}{pref("minimum_c", "舒適範圍下限（°C）", 16, 30, .5)}{pref("maximum_c", "舒適範圍上限（°C）", 16, 30, .5)}{pref("duration_minutes", "冷氣運轉時間（分鐘）", 15, 1440)}{pref("washer_hours", "洗衣完成期限（小時）", 1, 24)}{pref("quiet_start", "安靜時段開始（午夜後分鐘）", 0, 1439)}{pref("quiet_end", "安靜時段結束（午夜後分鐘）", 0, 1439)}</div><button className="primary-action" disabled={busy} onClick={() => submit("assistant_preferences", prefs)}>儲存偏好</button></article><article className="system-card settings-card"><h2>費率與每月基準</h2><label className="setting-field">費率方案<select value={plan} onChange={e => setPlan(e.target.value)}>{Object.entries(value.tariff_plans).map(([id, name]) => <option value={id} key={id}>{name}</option>)}</select></label><div className="baseline-list">{Object.entries(baseline).sort(([a], [b]) => a.localeCompare(b)).map(([month, amount]) => <label className="baseline-row" key={month}><span>{month}</span><input aria-label={`${month}基準用電`} type="number" min="0" step="0.1" value={amount} onChange={e => setBaseline(current => ({ ...current, [month]: e.target.value }))} /><span>kWh</span><button type="button" aria-label={`刪除 ${month} 基準`} onClick={() => setBaseline(current => Object.fromEntries(Object.entries(current).filter(([key]) => key !== month)))}>移除</button></label>)}</div><div className="baseline-add"><label className="setting-field">月份<input aria-label="新增基準月份" type="month" value={baselineMonth} onChange={e => setBaselineMonth(e.target.value)} /></label><label className="setting-field">基準用電（kWh）<input aria-label="新增基準用電" type="number" min="0" step="0.1" value={baselineValue} onChange={e => setBaselineValue(e.target.value)} /></label><button type="button" className="secondary-action" onClick={() => { if (baselineMonth && baselineValue !== "") setBaseline(current => ({ ...current, [baselineMonth]: baselineValue })); }}>新增月份</button></div><p className="fine-print">資料來源：{value.tariff_source.source_url} · 更新時間：{value.tariff_source.retrieved_at || "內建資料"}</p><button className="primary-action" disabled={busy} onClick={() => submit("tariff_settings", { plan, baseline_kwh: Object.fromEntries(Object.entries(baseline).filter(([, amount]) => amount !== "").map(([month, amount]) => [month, Number(amount)])) })}>儲存費率設定</button><button disabled={busy} className="secondary-action" onClick={() => submit("refresh_tariff_source", {})}>更新台電費率來源</button></article><article className="system-card settings-card"><h2>展示時鐘</h2><p>有未完成任務時無法切換時鐘倍率。</p><label className="setting-field">時間倍率<select value={speed} onChange={e => setSpeed(Number(e.target.value))}>{[0.1, 1, 2, 10, 60, 100].map(n => <option key={n} value={n}>{n}×</option>)}</select></label><button className="primary-action" disabled={busy || value.active_tasks.length > 0} onClick={() => submit("clock", { speed })}>套用時鐘</button></article></>}</section>;
}

type McpCatalog = {
  protocol: string;
  protocol_version: string;
  latest_protocol_version?: string;
  server: { name: string; version: string };
  resources: { uri: string; name: string; description?: string; mimeType?: string }[];
  tools_by_role: Record<string, { name: string; description?: string; inputSchema?: Record<string, unknown> }[]>;
};

export function DiagnosticsView() {
  const { value, error } = useResource<{ services: Record<string, string>; mcp_catalog?: McpCatalog; requests: Record<string, unknown>[]; agent_runs: Record<string, unknown>[]; tool_logs: Record<string, unknown>[]; audit: Record<string, unknown>[]; commands: Record<string, unknown>[] }>("/api/diagnostics");
  const catalog = value?.mcp_catalog;
  const resources = catalog?.resources ?? [];
  const roleEntries = Object.entries(catalog?.tools_by_role ?? {});
  return <section className="system-view"><div className="breadcrumb">家庭管理　›　診斷</div><h1>系統診斷</h1><p className="system-intro">本機服務狀態與最近操作紀錄。</p>{error && <div className="service-warning">{error}</div>}<div className="system-cards">{Object.entries(value?.services ?? {}).map(([key, val]) => <article className="system-card" key={key}><span>{key}</span><strong className="diagnostic-value">{val}</strong></article>)}</div>{catalog && <details className="diagnostic-section"><summary>MCP 協定目錄（Resources &amp; Role-Scoped Tools：{resources.length} Resources / {roleEntries.length} Roles）</summary><div className="mcp-catalog"><p className="fine-print">Server：{catalog.server.name} v{catalog.server.version} · 協定版本：{catalog.protocol_version}{catalog.latest_protocol_version ? `（SDK 最新支援 ${catalog.latest_protocol_version}）` : ""}</p><div className="mcp-block"><h3>MCP Resources</h3><ul>{resources.map(res => <li key={res.uri}><code>{res.uri}</code> — {res.description || res.name}</li>)}</ul></div><div className="mcp-block"><h3>Role-Scoped MCP Tools</h3><div className="mcp-role-grid">{roleEntries.map(([role, tools]) => <div className="mcp-role-card" key={role}><strong>{role}（{tools.length}）</strong>{tools.length === 0 ? <p className="fine-print">無直接控制工具（僅負責指令執行與狀態回報）</p> : <ul>{tools.map(tool => <li key={tool.name}><code>{tool.name}</code>：{tool.description || ""}</li>)}</ul>}</div>)}</div></div></div></details>}{([ ["設備命令", value?.commands], ["助理工作", value?.agent_runs], ["工具紀錄", value?.tool_logs], ["操作稽核", value?.audit], ["請求佇列", value?.requests] ] as [string, Record<string, unknown>[] | undefined][]).map(([title, rows]) => <details className="diagnostic-section" key={title}><summary>{title}（{rows?.length ?? 0}）</summary><pre>{JSON.stringify(rows ?? [], null, 2)}</pre></details>)}</section>;
}

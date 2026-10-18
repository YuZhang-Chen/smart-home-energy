export type LiveId = "ac-1" | "washer-1";
export type DeviceId = LiveId | "bulb-1" | "tv-1" | "curtain-1" | "fan-1";
export interface SceneDevice {
  id: DeviceId;
  name: string;
  english: string;
  location: string;
  source: "service" | "local" | "display";
  capability: "plan" | "light" | "info";
}
export const devices: SceneDevice[] = [
  {
    id: "ac-1",
    name: "冷氣",
    english: "AIR CONDITIONER",
    location: "客廳 · 後牆上方",
    source: "service",
    capability: "plan",
  },
  {
    id: "washer-1",
    name: "洗衣機",
    english: "WASHING MACHINE",
    location: "客廳 · 側邊家事區",
    source: "service",
    capability: "plan",
  },
  {
    id: "bulb-1",
    name: "智慧燈泡",
    english: "SMART LIGHT",
    location: "客廳 · 吊燈",
    source: "local",
    capability: "light",
  },
  {
    id: "tv-1",
    name: "電視",
    english: "TELEVISION",
    location: "客廳 · 電視櫃",
    source: "display",
    capability: "info",
  },
  {
    id: "curtain-1",
    name: "窗簾",
    english: "CURTAINS",
    location: "客廳 · 左側落地窗",
    source: "display",
    capability: "info",
  },
  {
    id: "fan-1",
    name: "電風扇",
    english: "FAN",
    location: "客廳 · 右側",
    source: "display",
    capability: "info",
  },
];
export const labels: Record<string, string> = {
  idle: "待機",
  needs_details: "待補充條件",
  pending: "待核准",
  scheduled: "已排程",
  starting: "啟動確認中",
  running: "運轉中",
  stopping: "中止確認中",
  completed: "已完成",
  cancelled: "已取消",
  aborted: "已中止",
  failed: "設備異常",
  unknown: "狀態待查核",
  resolved: "異常已解除",
  fault: "設備故障",
};
export interface ACConstraints {
  start: string;
  end: string;
  target_c: number;
  minimum_c: number;
  maximum_c: number;
  ambient_c: number;
  warming_seconds: number;
  device_id: "ac-1";
}
export interface WasherConstraints {
  earliest: string;
  deadline: string;
  quiet_start: number | null;
  quiet_end: number | null;
  device_id: "washer-1";
}
export interface Program {
  name: string;
  stages: { name: string; seconds: number; power_w: number }[];
}
export interface Quote {
  energy_kwh: number;
  cost: number;
  tariff: { official: unknown };
}
export interface WashPlan extends Quote {
  start: string;
  end: string;
  program: Program;
}
export interface ACPlan extends Quote {
  constraints: ACConstraints;
  comfortable_after_seconds: number | null;
  start_on_approval: boolean;
}
export interface Task {
  task_id: string;
  device_id: LiveId;
  version: number;
  status: string;
  message: string;
  run_id: string | null;
  constraints: WasherConstraints | null;
  ac_constraints: ACConstraints | null;
  plan: WashPlan | null;
  ac_plan: ACPlan | null;
}
export interface DeviceState {
  device_id: LiveId;
  observed_at: string;
  logical_at: string;
  clock_id: string;
  task_id: string | null;
  run_id: string | null;
  status: string;
  stage: string;
  progress: number;
  power_w: number;
  energy_kwh: number;
  cost: number;
  temperature_c?: number;
  target_c?: number;
}
export interface DeviceSnapshot {
  state: DeviceState | null;
  fresh: boolean;
  active_task: Task | null;
  latest_task: Task | null;
}
export interface Home {
  server_time: string;
  assistant_url: string;
  clock: { now: string; speed: number; clock_id: string };
  devices: Record<LiveId, DeviceSnapshot>;
  preferences: {
    target_c: number;
    minimum_c: number;
    maximum_c: number;
    duration_minutes: number;
    washer_hours: number;
    quiet_start: number;
    quiet_end: number;
  };
  program: Program;
}
export interface Command {
  request_id: string;
  kind: string;
  payload: Record<string, unknown>;
}
export interface RequestStatus {
  request_id: string;
  kind: string;
  status: "pending" | "done" | "error";
  result: string | null;
}

export function isFresh(
  snapshot: DeviceSnapshot | undefined,
  realNow: number,
): boolean {
  if (!snapshot?.state) return false;
  const age = realNow - Date.parse(snapshot.state.observed_at);
  return age >= -2000 && age <= 10_000;
}
export function isAnimating(
  snapshot: DeviceSnapshot | undefined,
  realNow: number,
  clockId?: string,
): boolean {
  const task = snapshot?.active_task,
    state = snapshot?.state;
  return !!(
    task &&
    state &&
    isFresh(snapshot, realNow) &&
    state.clock_id === clockId &&
    task.status === "running" &&
    state.status === "running" &&
    state.task_id === task.task_id &&
    state.run_id &&
    state.run_id === task.run_id
  );
}
export const localInput = (iso: string): string =>
  new Date(Date.parse(iso) + 8 * 3600_000).toISOString().slice(0, 16);
export const toISO = (input: string): string =>
  new Date(`${input}:00+08:00`).toISOString();
export const dateLabel = (value: string): string =>
  new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));

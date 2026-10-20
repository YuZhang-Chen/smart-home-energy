export type TimeMode = "real" | "manual" | "simulation";

export type DayNightState = {
  mode: TimeMode;
  manualMinute: number;
  anchorMinute: number;
  anchorTime: number;
  playing: boolean;
};

export type DayNightLighting = {
  sky: string;
  window: string;
  ambientColor: string;
  hemisphereSky: string;
  hemisphereGround: string;
  sunColor: string;
  ambientIntensity: number;
  hemisphereIntensity: number;
  sunIntensity: number;
  sunPosition: [number, number, number];
  phase: string;
};

export const SIMULATION_DURATION_MS = 120_000;
const DAY_MINUTES = 24 * 60;

export function localMinute(date: Date): number {
  return date.getHours() * 60 + date.getMinutes();
}

export function displayedMinute(
  clock: DayNightState,
  wallTime = Date.now(),
  monotonicTime = performance.now(),
): number {
  if (clock.mode === "real") return localMinute(new Date(wallTime));
  if (clock.mode === "manual" || !clock.playing) return clock.manualMinute;
  const elapsed = Math.max(0, monotonicTime - clock.anchorTime);
  return (
    (clock.anchorMinute +
      (elapsed / SIMULATION_DURATION_MS) * DAY_MINUTES) %
    DAY_MINUTES
  );
}

export function formatTime(minute: number): string {
  const normalized = Math.floor(minute) % DAY_MINUTES;
  return `${String(Math.floor(normalized / 60)).padStart(2, "0")}:${String(normalized % 60).padStart(2, "0")}`;
}

export function phaseForMinute(minute: number): string {
  const hour = (minute % DAY_MINUTES) / 60;
  if (hour < 6) return "夜晚";
  if (hour < 9) return "清晨";
  if (hour < 17) return "白天";
  if (hour < 20) return "黃昏";
  return "夜晚";
}

type ColorKey = { minute: number; color: string };
const skyKeys: ColorKey[] = [
  { minute: 0, color: "#24344f" },
  { minute: 240, color: "#394d6d" },
  { minute: 360, color: "#edaa7e" },
  { minute: 540, color: "#c8dfed" },
  { minute: 720, color: "#c5e0f0" },
  { minute: 960, color: "#d9d7d2" },
  { minute: 1080, color: "#e59b76" },
  { minute: 1260, color: "#625c82" },
  { minute: 1440, color: "#24344f" },
];

function hexToRgb(hex: string): [number, number, number] {
  const value = Number.parseInt(hex.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function rgbToHex(rgb: [number, number, number]): string {
  return `#${rgb.map((channel) => Math.round(channel).toString(16).padStart(2, "0")).join("")}`;
}

function smooth(t: number): number {
  return t * t * (3 - 2 * t);
}

function colorAt(minute: number): string {
  const time = ((minute % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES;
  const index = skyKeys.findIndex((key, i) => key.minute <= time && time <= skyKeys[i + 1]?.minute);
  const left = skyKeys[Math.max(0, index)];
  const right = skyKeys[Math.max(1, index + 1)];
  const t = smooth((time - left.minute) / (right.minute - left.minute));
  const a = hexToRgb(left.color), b = hexToRgb(right.color);
  return rgbToHex([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]);
}

function daylightAt(minute: number): number {
  const hour = ((minute % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES / 60;
  if (hour <= 6 || hour >= 18) return 0;
  return Math.sin(((hour - 6) / 12) * Math.PI);
}

export function calculateDayNight(minute: number): DayNightLighting {
  const time = ((minute % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES;
  const daylight = daylightAt(time);
  const sunPhase = Math.max(0, Math.min(1, (time / 60 - 6) / 12));
  const sunHeight = Math.sin(Math.PI * sunPhase);
  const hour = time / 60;
  const sunColor = hour < 8 || hour > 16 ? "#ffd0a0" : hour < 9 || hour > 15 ? "#ffe3bd" : "#fff2d4";
  const sky = colorAt(time);
  return {
    sky,
    window: sky,
    ambientColor: time < 360 || time > 1080 ? "#c9d2e6" : "#fff6e6",
    hemisphereSky: sky,
    hemisphereGround: time < 360 || time > 1080 ? "#7c8291" : "#c3b59c",
    sunColor,
    ambientIntensity: 0.2 + daylight * 0.55,
    hemisphereIntensity: 0.35 + daylight * 0.9,
    sunIntensity: Math.pow(daylight, 0.72) * 2.4,
    sunPosition: [-10 * Math.cos(Math.PI * sunPhase), Math.max(0.35, 1 + 11 * sunHeight), 5],
    phase: phaseForMinute(time),
  };
}

export function clockWithMode(
  current: DayNightState,
  mode: TimeMode,
  now: number,
  performanceTime: number,
): DayNightState {
  const minute = displayedMinute(current, now, performanceTime);
  if (mode === "real") return { ...current, mode };
  if (mode === "manual") return { ...current, mode, manualMinute: Math.floor(minute) };
  return {
    ...current,
    mode,
    manualMinute: Math.floor(minute),
    anchorMinute: minute,
    anchorTime: performanceTime,
    playing: true,
  };
}

export function setManualMinute(current: DayNightState, minute: number): DayNightState {
  const value = Math.max(0, Math.min(1439, Math.round(minute)));
  return { ...current, mode: "manual", manualMinute: value, anchorMinute: value, playing: false };
}

export function setPausedSimulationMinute(
  current: DayNightState,
  minute: number,
  performanceTime = performance.now(),
): DayNightState {
  const value = Math.max(0, Math.min(1439, Math.round(minute)));
  return { ...current, mode: "simulation", manualMinute: value, anchorMinute: value, anchorTime: performanceTime, playing: false };
}

export function toggleSimulation(current: DayNightState, performanceTime: number): DayNightState {
  const minute = displayedMinute(current, Date.now(), performanceTime);
  return { ...current, mode: "simulation", manualMinute: Math.floor(minute), anchorMinute: minute, anchorTime: performanceTime, playing: !current.playing };
}

export function replaySimulation(current: DayNightState, performanceTime: number): DayNightState {
  return { ...current, mode: "simulation", manualMinute: 0, anchorMinute: 0, anchorTime: performanceTime, playing: true };
}

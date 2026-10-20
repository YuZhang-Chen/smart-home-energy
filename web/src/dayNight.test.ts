import { describe, expect, it } from "vitest";
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
} from "./dayNight";

const initial: DayNightState = {
  mode: "real",
  manualMinute: 0,
  anchorMinute: 0,
  anchorTime: 0,
  playing: false,
};

describe("day and night clock", () => {
  it("reads local browser time and follows the selected mode", () => {
    const local = new Date(2026, 9, 18, 7, 42);
    expect(localMinute(local)).toBe(7 * 60 + 42);
    expect(displayedMinute(initial, local.getTime(), 0)).toBe(7 * 60 + 42);
    const manual = clockWithMode(initial, "manual", local.getTime(), 100);
    expect(manual.manualMinute).toBe(7 * 60 + 42);
    expect(displayedMinute(manual, local.getTime() + 100_000, 100_000)).toBe(7 * 60 + 42);
    expect(clockWithMode(manual, "real", local.getTime() + 60_000, 100_000).mode).toBe("real");
  });

  it("runs a 120 second loop, wraps midnight, pauses, resumes, and replays", () => {
    let clock = setManualMinute(initial, 1439);
    clock = clockWithMode(clock, "simulation", 0, 50_000);
    expect(displayedMinute(clock, 0, 50_000 + 5_000)).toBeCloseTo(59, 4);
    expect(displayedMinute(clock, 0, 50_000 + 120_000)).toBeCloseTo(1439, 4);
    clock = toggleSimulation(clock, 55_000);
    expect(clock.playing).toBe(false);
    clock = setPausedSimulationMinute(clock, 23 * 60, 55_000);
    expect(clock.mode).toBe("simulation");
    const pausedAt = displayedMinute(clock, 0, 100_000);
    expect(pausedAt).toBe(clock.manualMinute);
    clock = toggleSimulation(clock, 56_000);
    expect(clock.playing).toBe(true);
    expect(displayedMinute(clock, 0, 86_000)).toBeCloseTo((clock.anchorMinute + 360) % 1440, 4);
    clock = replaySimulation(clock, 90_000);
    expect(clock.playing).toBe(true);
    expect(displayedMinute(clock, 0, 90_000)).toBe(0);
  });

  it("uses elapsed time so the result is independent of frame rate", () => {
    const clock = clockWithMode(setManualMinute(initial, 360), "simulation", 0, 12_000);
    const at60fps = displayedMinute(clock, 0, 12_000 + 47_500);
    const at30fps = displayedMinute(clock, 0, 12_000 + 47_500);
    expect(at60fps).toBe(at30fps);
    expect(at60fps).toBeCloseTo(930, 4);
  });
});

describe("natural light", () => {
  it.each([
    [0, "夜晚", 0],
    [360, "清晨", 0],
    [540, "白天", 1],
    [720, "白天", 1],
    [1080, "黃昏", 0],
    [1260, "夜晚", 0],
  ])("calculates the scene at minute %i", (minute, phase, daylight) => {
    const light = calculateDayNight(minute);
    expect(light.phase).toBe(phase);
    if (daylight === 0) expect(light.sunIntensity).toBe(0);
    else expect(light.sunIntensity).toBeGreaterThan(0);
    expect(light.sky).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it("keeps midnight continuous and the sun rises across the room", () => {
    const beforeMidnight = calculateDayNight(1439);
    const midnight = calculateDayNight(0);
    const channelDiff = beforeMidnight.sky.match(/[a-f\d]{2}/gi)!.map((c, i) => Math.abs(parseInt(c, 16) - parseInt(midnight.sky.match(/[a-f\d]{2}/gi)![i], 16)));
    expect(Math.max(...channelDiff)).toBeLessThan(2);
    expect(calculateDayNight(360).sunPosition[0]).toBeLessThan(0);
    expect(calculateDayNight(720).sunPosition[1]).toBeGreaterThan(calculateDayNight(360).sunPosition[1]);
    expect(calculateDayNight(1080).sunPosition[0]).toBeGreaterThan(0);
  });
});

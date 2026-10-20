import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { useCommands } from "./api";
import DevicePanel, { PlanForm } from "./DevicePanel";
import {
  devices,
  isAnimating,
  isFresh,
  type DeviceSnapshot,
  type Home,
  type Task,
} from "./types";

vi.mock("./Scene", () => ({
  default: ({ lightOn }: { lightOn: boolean }) => (
    <div data-testid="scene" data-light={String(lightOn)} />
  ),
}));

const stamp = "2026-10-18T06:00:00+00:00";
function fixture(): Home {
  return {
    server_time: stamp,
    assistant_url: "http://127.0.0.1:8501",
    clock: { now: stamp, clock_id: "clock", speed: 1 },
    preferences: {
      target_c: 27,
      minimum_c: 26,
      maximum_c: 28,
      duration_minutes: 60,
      washer_hours: 24,
      quiet_start: 1380,
      quiet_end: 420,
    },
    program: {
      name: "標準",
      stages: [
        { name: "洗滌", seconds: 1800, power_w: 200 },
        { name: "漂洗", seconds: 900, power_w: 100 },
        { name: "脫水", seconds: 900, power_w: 400 },
      ],
    },
    devices: {
      "ac-1": {
        state: null,
        fresh: false,
        active_task: null,
        latest_task: null,
      },
      "washer-1": {
        state: null,
        fresh: false,
        active_task: null,
        latest_task: null,
      },
    },
  };
}
function running(): DeviceSnapshot {
  return {
    fresh: true,
    latest_task: null,
    state: {
      device_id: "ac-1",
      observed_at: stamp,
      logical_at: stamp,
      clock_id: "clock",
      task_id: "task",
      run_id: "run",
      status: "running",
      stage: "冷房",
      progress: 0.5,
      power_w: 320,
      energy_kwh: 0.1,
      cost: 0.6,
      temperature_c: 28.1,
    },
    active_task: {
      task_id: "task",
      device_id: "ac-1",
      version: 3,
      status: "running",
      message: "",
      run_id: "run",
      constraints: null,
      ac_constraints: null,
      plan: null,
      ac_plan: null,
    },
  };
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("telemetry and animation contract", () => {
  it("animates only a fresh matching running task on the same clock", () => {
    const snapshot = running(),
      now = Date.parse(stamp);
    expect(isAnimating(snapshot, now, "clock")).toBe(true);
    expect(isAnimating(snapshot, now + 10_001, "clock")).toBe(false);
    expect(isAnimating(snapshot, now - 2001, "clock")).toBe(false);
    expect(isAnimating(snapshot, now, "different-clock")).toBe(false);
    snapshot.state!.run_id = "old-run";
    expect(isAnimating(snapshot, now, "clock")).toBe(false);
    snapshot.state!.run_id = "run";
    snapshot.active_task!.status = "stopping";
    expect(isAnimating(snapshot, now, "clock")).toBe(false);
    expect(isFresh(undefined, now)).toBe(false);
  });
  it("keeps the last measured value when telemetry becomes stale", () => {
    const home = fixture();
    home.devices["ac-1"] = running();
    render(
      <DevicePanel
        device={devices[0]}
        home={home}
        realNow={Date.parse(stamp) + 11000}
        lightOn
        setLightOn={vi.fn()}
        submit={vi.fn()}
        busy={false}
        connected
      />,
    );
    expect(
      screen.getByText("設備回報已過期，保留最後數值"),
    ).toBeInTheDocument();
    expect(screen.getByText("28.1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "關閉冷氣" })).toBeDisabled();
  });
});

describe("planning and approval", () => {
  it("retains edited AC fields across polling and submits Taipei times", () => {
    const home = fixture(),
      submit = vi.fn();
    const { rerender } = render(
      <PlanForm
        device={devices[0]}
        home={home}
        task={null}
        submit={submit}
        busy={false}
      />,
    );
    fireEvent.change(screen.getByLabelText(/目標溫度/), {
      target: { value: "26.5" },
    });
    rerender(
      <PlanForm
        device={devices[0]}
        home={{
          ...home,
          clock: { ...home.clock, now: "2026-10-18T07:00:00Z" },
        }}
        task={null}
        submit={submit}
        busy={false}
      />,
    );
    expect(screen.getByLabelText(/目標溫度/)).toHaveValue(26.5);
    expect(screen.getByLabelText("開始時間")).toHaveValue("2026-10-18T14:10");
    fireEvent.click(screen.getByRole("button", { name: "計算並提出計畫" }));
    expect(submit).toHaveBeenCalledWith(
      "create_ac_form",
      expect.objectContaining({
        constraints: expect.objectContaining({
          start: "2026-10-18T06:10:00.000Z",
          target_c: 26.5,
        }),
      }),
    );
  });
  it("submits disabled quiet periods as null and does not auto-approve", () => {
    const submit = vi.fn();
    render(
      <PlanForm
        device={devices[1]}
        home={fixture()}
        task={null}
        submit={submit}
        busy={false}
      />,
    );
    fireEvent.click(screen.getByLabelText("避開安靜時段"));
    fireEvent.click(screen.getByRole("button", { name: "計算並提出計畫" }));
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledWith(
      "create_form",
      expect.objectContaining({
        constraints: expect.objectContaining({
          quiet_start: null,
          quiet_end: null,
        }),
      }),
    );
  });
  it("requires a separate confirmation before stopping a running task", () => {
    const home = fixture(),
      submit = vi.fn();
    home.devices["ac-1"] = running();
    render(
      <DevicePanel
        device={devices[0]}
        home={home}
        realNow={Date.parse(stamp)}
        lightOn
        setLightOn={vi.fn()}
        submit={submit}
        busy={false}
        connected
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "關閉冷氣" }));
    expect(submit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "確認停止" }));
    expect(submit).toHaveBeenCalledWith("abort", {
      task_id: "task",
      expected_version: 3,
      confirmed: true,
    });
  });
  it("sends the displayed quote cap and version only on explicit approval", () => {
    const home = fixture(),
      submit = vi.fn();
    const task: Task = {
      task_id: "wash",
      version: 2,
      status: "pending",
      device_id: "washer-1",
      message: "",
      run_id: null,
      constraints: null,
      ac_constraints: null,
      ac_plan: null,
      plan: {
        cost: 1.1,
        energy_kwh: 0.2,
        start: stamp,
        end: "2026-10-18T07:00:00Z",
        program: home.program,
        tariff: { official: null },
      },
    };
    home.devices["washer-1"].active_task = task;
    render(
      <DevicePanel
        device={devices[1]}
        home={home}
        realNow={Date.parse(stamp)}
        lightOn
        setLightOn={vi.fn()}
        submit={submit}
        busy={false}
        connected
      />,
    );
    expect(submit).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("核准費用上限"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "核准洗衣計畫" }));
    expect(submit).toHaveBeenCalledWith("approve", {
      task_id: "wash",
      expected_version: 2,
      max_cost: 2,
    });
  });
});

describe("accessible scene controls", () => {
  it("selects all six devices and synchronizes the light with the scene", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () => new Response(JSON.stringify(fixture()), { status: 200 }),
      ),
    );
    const user = userEvent.setup();
    render(<App />);
    await screen.findByLabelText("開始時間");
    const list = screen.getByRole("region", { name: "客廳設備清單" });
    expect(list.querySelectorAll("button")).toHaveLength(6);
    expect(
      screen.queryByRole("button", { name: /智慧插座/ }),
    ).not.toBeInTheDocument();
    const bulb = screen.getByRole("button", { name: /智慧燈泡 燈光已開啟/ });
    bulb.focus();
    await user.keyboard("{Enter}");
    const toggle = screen.getByRole("switch", { name: "客廳燈泡" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    await waitFor(() =>
      expect(screen.getByTestId("scene")).toHaveAttribute(
        "data-light",
        "false",
      ),
    );
    for (const name of ["電視", "窗簾", "電風扇"]) {
      await user.click(
        screen.getByRole("button", { name: `${name} 資訊展示` }),
      );
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
      expect(screen.getByText(/尚未串接設備服務/)).toBeInTheDocument();
    }
    expect(screen.getByText("FAN")).toBeInTheDocument();
    expect(screen.getByText("客廳 · 右側")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "電風扇 資訊展示" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("changes natural light time without changing the independent bulb or submitting equipment requests", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify(fixture()), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByLabelText("開始時間");
    await user.click(screen.getByRole("button", { name: /智慧燈泡 燈光已開啟/ }));
    const bulb = screen.getByRole("switch", { name: "客廳燈泡" });
    expect(bulb).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: /手動時間/ }));
    fireEvent.change(screen.getByRole("slider", { name: "客廳時間" }), {
      target: { value: "1439" },
    });
    expect(screen.getAllByText("23:59").length).toBeGreaterThan(0);
    expect(bulb).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: /加速模擬/ }));
    expect(screen.getByRole("button", { name: "暫停" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "從 00:00 重播" }));
    expect(bulb).toHaveAttribute("aria-checked", "true");
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });
});

describe("durable request retries", () => {
  it("reuses one request ID after a lost response and prevents duplicate clicks", async () => {
    vi.useFakeTimers();
    const bodies: string[] = [],
      refresh = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_path: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          bodies.push(String(init.body));
          if (bodies.length === 1) throw new TypeError("lost response");
          return new Response(JSON.stringify({ request_id: "same" }), {
            status: 202,
          });
        }
        return new Response(
          JSON.stringify({ status: "done", result: "已處理" }),
          { status: 200 },
        );
      }),
    );
    const { result, unmount } = renderHook(() => useCommands(refresh));
    await act(async () => {
      result.current.submit("cancel", { task_id: "test", expected_version: 1 });
      result.current.submit("cancel", { task_id: "test", expected_version: 1 });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(bodies).toHaveLength(2);
    expect(JSON.parse(bodies[0]).request_id).toBe(
      JSON.parse(bodies[1]).request_id,
    );
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(result.current.pending).toBeNull();
    unmount();
  });
});

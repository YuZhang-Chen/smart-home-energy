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
import App, { CurtainActionCard, LightActionCard, ProposalCard, TVActionCard } from "./App";
import { DiagnosticsView, SettingsView } from "./SystemViews";
import CurtainControl from "./CurtainControl";
import TVControl from "./TVControl";
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

vi.mock("@copilotkit/react-core/v2", () => ({
  CopilotChatInput: () => null,
  CopilotSidebar: () => null,
  useConfigureSuggestions: () => undefined,
  useRenderTool: () => undefined,
}));

vi.mock("./Scene", () => ({
  default: ({ lightOn }: { lightOn: boolean }) => (
    <div data-testid="scene" data-light={String(lightOn)} />
  ),
}));

it("shows confirmed light feedback and requested target in the conversation card", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({
    status: "done", result: "燈泡已達到設定並回報狀態",
    action: { status: "confirmed" }, target: { power: true, brightness_percent: 40, kelvin: 2700 },
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  const view = render(<LightActionCard requestId="request-1" />);
  expect(await screen.findByText("設備已確認")).toBeInTheDocument();
  expect(screen.getByText(/亮度 40%/)).toHaveTextContent("色溫 2700K");
  view.unmount();
  fetchMock.mockRestore();
});

const stamp = "2026-10-18T06:00:00+00:00";
function fixture(): Home {
  return {
    server_time: stamp,
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
      "bulb-1": {
        state: {
          device_id: "bulb-1",
          observed_at: stamp,
          boot_id: "bulb-boot",
          sequence: 1,
          state_version: 1,
          power: true,
          level: 254,
          mired: 370,
          transitioning: false,
          fault: null,
        },
        fresh: true,
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
        setLightOn={vi.fn()} tvPower={false} onTVPowerChange={vi.fn()}
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
  it("shows the latest plan only after review and permits editing before approval", async () => {
    const home = fixture();
    const constraints = { start: stamp, end: "2026-10-18T07:00:00+00:00", target_c: 27, minimum_c: 26, maximum_c: 28, ambient_c: 32, warming_seconds: 1800, device_id: "ac-1" as const };
    const task: Task = { task_id: "card", device_id: "ac-1", version: 2, status: "pending", message: "", run_id: null, constraints: null, ac_constraints: constraints, plan: null, ac_plan: { constraints, energy_kwh: 0.5, cost: 3, tariff: { official: null }, comfortable_after_seconds: 0, start_on_approval: false } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => task }));
    const submit = vi.fn();
    render(<ProposalCard taskId="card" version={1} home={home} submit={submit} busy={false} />);
    expect(await screen.findByText(/計畫已更新/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "確認計畫" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "載入最新版本" }));
    expect(screen.getByText(/目標 27°C/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "修改計畫" }));
    expect(screen.getByLabelText("自然語言補充")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "確認計畫" }));
    expect(submit).toHaveBeenCalledWith("approve", { task_id: "card", expected_version: 2 });
  });
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
        setLightOn={vi.fn()} tvPower={false} onTVPowerChange={vi.fn()}
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
        setLightOn={vi.fn()} tvPower={false} onTVPowerChange={vi.fn()}
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
  it("submits a 40% warm bulb target while rendering only reported state", () => {
    const home = fixture();
    home.devices["bulb-1"].state!.power = false;
    const submit = vi.fn();
    const props = {
      device: devices.find((device) => device.id === "bulb-1")!,
      home,
      realNow: Date.parse(stamp),
      lightOn: false,
      setLightOn: vi.fn(),
      tvPower: false,
      onTVPowerChange: vi.fn(),
      submit,
      busy: false,
      connected: true,
    };
    const { rerender } = render(<DevicePanel {...props} />);
    fireEvent.change(screen.getByRole("slider", { name: "燈泡亮度" }), {
      target: { value: "40" },
    });
    fireEvent.click(screen.getByRole("button", { name: "套用照明設定" }));
    expect(submit).toHaveBeenCalledWith("set_bulb", {
      device_id: "bulb-1",
      power: true,
      brightness_percent: 40,
      kelvin: 2700,
    });
    expect(screen.getByRole("switch", { name: "客廳燈泡" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("已關閉 · 保留環境光")).toBeInTheDocument();
    const reported = fixture();
    reported.devices["bulb-1"].state = {
      ...reported.devices["bulb-1"].state!,
      power: true,
      level: 102,
      state_version: 2,
    };
    rerender(<DevicePanel {...props} home={reported} lightOn />);
    expect(screen.getByRole("switch", { name: "客廳燈泡" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/已開啟 · 40%/)).toBeInTheDocument();
  });

  it("keeps the last bulb reading visible but disables control when telemetry is stale", () => {
    const submit = vi.fn();
    render(<DevicePanel device={devices.find((device) => device.id === "bulb-1")!}
      home={fixture()} realNow={Date.parse(stamp) + 11_000} lightOn
      setLightOn={vi.fn()} tvPower={false} onTVPowerChange={vi.fn()} submit={submit} busy={false} connected />);
    expect(screen.getByText("設備回報已過期，保留最後數值")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "客廳燈泡" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "套用照明設定" })).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("selects all six devices and synchronizes the light with the scene", async () => {
    let bulbPower = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (init?.method === "POST") {
          bulbPower = false;
          return new Response(JSON.stringify({ request_id: "test" }), { status: 202 });
        }
        if (path.startsWith("/api/requests/"))
          return new Response(JSON.stringify({ status: "done", result: "燈泡已達到設定" }), { status: 200 });
        const home = fixture();
        home.devices["bulb-1"].state!.power = bulbPower;
        return new Response(JSON.stringify(home), { status: 200 });
      }),
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /冷氣/ }));
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
    await user.click(screen.getByRole("button", { name: /電視/ }));
    expect(screen.getByText("LG Smart TV")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開啟電視" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /窗簾/ }));
    expect(screen.getByText("智慧窗簾")).toBeInTheDocument();
    expect(screen.getByText("Curtain Agent")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "電風扇 資訊展示" }),
    );
    expect(screen.getByRole("heading", { name: "電風扇" })).toBeInTheDocument();
    expect(screen.getByText(/尚未串接設備服務/)).toBeInTheDocument();
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
    await user.click(await screen.findByRole("button", { name: /冷氣/ }));
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

describe("diagnostics MCP catalog", () => {
  it("renders mcp_server status and role-scoped MCP catalog", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          services: { api: "online", mcp_server: "online (MCP SDK)" },
          mcp_catalog: {
            protocol: "MCP",
            protocol_version: "2024-11-05",
            server: { name: "smart-home-mcp", version: "1.0.0" },
            resources: [
              { uri: "home://state", name: "home_state", description: "全屋設備狀態" },
            ],
            tools_by_role: {
              "TV Agent": [{ name: "control_tv", description: "控制客廳電視" }],
            },
          },
          requests: [],
          agent_runs: [],
          tool_logs: [],
          audit: [],
          commands: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const view = render(<DiagnosticsView />);
    expect(await screen.findByText("online (MCP SDK)")).toBeInTheDocument();
    expect(screen.getByText(/MCP 協定目錄/)).toBeInTheDocument();
    expect(screen.getByText("home://state")).toBeInTheDocument();
    expect(screen.getByText("control_tv")).toBeInTheDocument();
    view.unmount();
    fetchMock.mockRestore();
  });
});

describe("lighting automation settings", () => {
  it("submits lighting_rule payload and shows resume only during manual_override", async () => {
    const user = userEvent.setup();
    const submit = vi.fn();
    let reason = "night";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (path) => {
      const url = String(path);
      if (url.includes("/api/lighting")) {
        return new Response(
          JSON.stringify({
            rule: {
              enabled: true,
              day_start: 360,
              night_start: 1080,
              brightness: 40,
              kelvin: 2700,
              presence_enabled: false,
              version: 2,
            },
            status: {
              window: "night",
              reason,
              override_until: reason === "manual_override" ? "2026-11-27T06:00:00+08:00" : null,
              next_boundary: "2026-11-27T06:00:00+08:00",
              last_request_id: null,
              last_request_status: null,
              last_error: null,
              evaluated_at: "2026-11-26T21:00:00+08:00",
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          preferences: { target_c: 26, minimum_c: 24, maximum_c: 28, duration_minutes: 120, washer_hours: 6, quiet_start: 1380, quiet_end: 420 },
          tariff: { plan: "simulated", baseline_kwh: {} },
          tariff_plans: { simulated: "模擬費率" },
          tariff_source: { source_url: "local", retrieved_at: "" },
          clock: { speed: 1 },
          active_tasks: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    const first = render(<SettingsView submit={submit} busy={false} />);
    expect(await screen.findByText("自動照明")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "恢復自動" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "儲存自動照明" }));
    expect(submit).toHaveBeenCalledWith("lighting_rule", {
      device_id: "bulb-1",
      expected_version: 2,
      enabled: true,
      day_start: 360,
      night_start: 1080,
      brightness: 40,
      kelvin: 2700,
      presence_enabled: false,
    });
    first.unmount();

    reason = "manual_override";
    const second = render(<SettingsView submit={submit} busy={false} />);
    const resumeBtn = await screen.findByRole("button", { name: "恢復自動" });
    await user.click(resumeBtn);
    expect(submit).toHaveBeenCalledWith("lighting_resume", { device_id: "bulb-1" });
    second.unmount();
    fetchMock.mockRestore();
  });
});

describe("CurtainControl Main Agent & durable request queue integration", () => {
  it("submits manual controls and schedule mutations through durable requests and routes natural language via /api/assistant/ag-ui", async () => {
    const user = userEvent.setup();
    const submit = vi.fn();
    const onRefresh = vi.fn();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/curtain/status")) {
        return new Response(
          JSON.stringify({ device_id: "curtain-1", position: 0, target: 0, status: "stopped", updated_at: stamp }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.endsWith("/api/curtain/schedules")) {
        return new Response(
          JSON.stringify([{ id: "sched-1", time: "07:00", position: 100, enabled: true }]),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.endsWith("/api/assistant/ag-ui")) {
        const sse = [
          `data: ${JSON.stringify({ type: "CUSTOM", name: "assistant_progress", value: { text: "Main Agent 正在委派 Curtain Agent" } })}\n\n`,
          `data: ${JSON.stringify({ type: "TEXT_MESSAGE_CONTENT", delta: "已停用客廳窗簾排程（07:00）。" })}\n\n`,
        ].join("");
        return new Response(sse, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });

    const view = render(
      <CurtainControl
        initialStatus={{ device_id: "curtain-1", position: 0, target: 0, status: "stopped", updated_at: stamp }}
        initialSchedules={[{ id: "sched-1", time: "07:00", position: 100, enabled: true }]}
        submit={submit}
        busy={false}
        onRefresh={onRefresh}
      />,
    );

    await user.click(screen.getByRole("button", { name: /半開\s*50%/ }));
    expect(submit).toHaveBeenCalledWith("curtain_scene", { device_id: "curtain-1", scene: "half" });

    await user.click(screen.getByRole("button", { name: "刪除" }));
    expect(submit).toHaveBeenCalledWith("curtain_schedule_delete", { device_id: "curtain-1", schedule_id: "sched-1" });

    const inputEl = screen.getByLabelText("輸入窗簾中文指令");
    await user.type(inputEl, "停用 07:00 排程");
    await user.click(screen.getByRole("button", { name: "送出窗簾指令" }));

    expect(await screen.findByText("已停用客廳窗簾排程（07:00）。")).toBeInTheDocument();
    expect(onRefresh).toHaveBeenCalled();

    const aguiCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/assistant/ag-ui"));
    expect(aguiCall).toBeDefined();
    const body = JSON.parse(String((aguiCall?.[1] as RequestInit)?.body ?? "{}"));
    expect(body.threadId).toBe("home");
    expect(body.messages[0].content).toContain("窗簾");

    view.unmount();
    fetchMock.mockRestore();
  });
});

describe("TVControl Main Agent & durable request queue integration", () => {
  it("submits manual TV controls through durable requests and routes natural language via /api/assistant/ag-ui", async () => {
    const user = userEvent.setup();
    const submit = vi.fn();
    const onPowerChange = vi.fn();
    const onRefresh = vi.fn();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/assistant/ag-ui")) {
        const sse = [
          `data: ${JSON.stringify({ type: "CUSTOM", name: "assistant_progress", value: { text: "Main Agent 正在委派 TV Agent" } })}\n\n`,
          `data: ${JSON.stringify({ type: "TEXT_MESSAGE_CONTENT", delta: "客廳電視已執行：切換至 HDMI2、音量調整至 30%。" })}\n\n`,
        ].join("");
        return new Response(sse, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });

    const view = render(
      <TVControl
        power={true}
        onPowerChange={onPowerChange}
        tvState={{
          device_id: "tv-1",
          power: true,
          volume: 25,
          muted: false,
          input: "HDMI1",
          playback: "stopped",
          updated_at: stamp,
        }}
        submit={submit}
        busy={false}
        onRefresh={onRefresh}
      />,
    );

    await user.click(screen.getByRole("button", { name: "切換靜音" }));
    expect(submit).toHaveBeenCalledWith("tv_control", { device_id: "tv-1", muted: true });

    await user.click(screen.getByRole("button", { name: "HDMI2" }));
    expect(submit).toHaveBeenCalledWith("tv_control", { device_id: "tv-1", input: "HDMI2" });

    await user.click(screen.getByRole("button", { name: "播放" }));
    expect(submit).toHaveBeenCalledWith("tv_control", { device_id: "tv-1", playback: "playing" });

    const inputEl = screen.getByLabelText("輸入電視指令");
    await user.type(inputEl, "太吵了音量調到 30");
    await user.click(screen.getByRole("button", { name: "送出指令" }));

    expect((await screen.findAllByText("客廳電視已執行：切換至 HDMI2、音量調整至 30%。")).length).toBeGreaterThan(0);
    expect(onRefresh).toHaveBeenCalled();

    const aguiCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/assistant/ag-ui"));
    expect(aguiCall).toBeDefined();
    const body = JSON.parse(String((aguiCall?.[1] as RequestInit)?.body ?? "{}"));
    expect(body.threadId).toBe("home");
    expect(body.messages[0].content).toContain("客廳電視");

    view.unmount();
    fetchMock.mockRestore();
  });

  it("renders TVActionCard and CurtainActionCard as clean status receipt cards without redundant buttons", () => {
    const home = {
      ...fixture(),
      devices: {
        ...fixture().devices,
        "tv-1": {
          state: {
            device_id: "tv-1" as const,
            power: true,
            volume: 35,
            muted: false,
            input: "HDMI2" as const,
            playback: "playing" as const,
            updated_at: stamp,
          },
          fresh: true,
          active_task: null,
          latest_task: null,
        },
        "curtain-1": {
          state: {
            device_id: "curtain-1" as const,
            position: 50,
            target: 50,
            status: "stopped" as const,
            updated_at: stamp,
          },
          fresh: true,
          active_task: null,
          latest_task: null,
          schedules: [{ id: "s1", time: "07:00", position: 100, enabled: true }],
        },
      },
    };

    const view = render(
      <>
        <TVActionCard summary="已切換至 HDMI2" home={home} />
        <CurtainActionCard summary="已拉開至 50%" home={home} />
      </>,
    );

    expect(screen.getByText(/運作中 · HDMI2 · 音量 35% · 播放中/)).toBeInTheDocument();
    expect(screen.getByText("已切換至 HDMI2")).toBeInTheDocument();
    expect(screen.getByText(/目前 50%（目標 50%） · 啟用排程 1 筆/)).toBeInTheDocument();
    expect(screen.getByText("已拉開至 50%")).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);

    view.unmount();
  });
});

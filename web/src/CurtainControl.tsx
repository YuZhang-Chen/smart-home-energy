import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  Blinds,
  Bot,
  CheckCircle2,
  Clock3,
  Send,
} from "lucide-react";
import type { CurtainSchedule, CurtainState } from "./types";
import "./curtain.css";

const API = "/api/curtain";

type SubmitFn = (kind: string, payload: Record<string, unknown>) => void;

interface CurtainControlProps {
  initialStatus?: CurtainState | null;
  initialSchedules?: CurtainSchedule[];
  submit?: SubmitFn;
  busy?: boolean;
  onRefresh?: () => void;
}

async function apiRequest<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || "API 請求失敗");
  }

  return data as T;
}

async function runViaMainAgent(
  text: string,
  onProgress: (msg: string) => void,
): Promise<string> {
  const runId = `curtain-${crypto.randomUUID()}`;
  const messageId = `msg-${runId}`;
  const content = /窗簾|窗帘|curtain|blinds/i.test(text) ? text : `窗簾：${text}`;

  const response = await fetch("/api/assistant/ag-ui", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      threadId: "home",
      runId,
      state: {},
      messages: [{ id: messageId, role: "user", content }],
      tools: [],
      context: [],
      forwardedProps: {},
    }),
  });

  if (!response.ok) {
    let detail = "Main Agent 請求失敗";
    try {
      const err = await response.json();
      if (typeof err?.detail === "string") detail = err.detail;
    } catch {
      // ignore non-json error body
    }
    throw new Error(detail);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    return "已送交 Main Agent 處理";
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let reply = "";
  let runError = "";

  const processChunk = (rawBlock: string) => {
    const dataLines = rawBlock
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim());
    if (dataLines.length === 0) return;
    const jsonText = dataLines.join("\n");
    try {
      const evt = JSON.parse(jsonText) as Record<string, unknown>;
      const type = String(evt.type ?? "");
      if (type === "CUSTOM" && evt.name === "assistant_progress") {
        const val = evt.value as { text?: string } | undefined;
        if (val?.text) onProgress(val.text);
      } else if (type === "TEXT_MESSAGE_CONTENT" && typeof evt.delta === "string") {
        reply += evt.delta;
      } else if (type === "RUN_ERROR" && typeof evt.message === "string") {
        runError = evt.message;
      }
    } catch {
      // ignore heartbeat / non-json lines
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split(/\r?\n\r?\n/);
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      processChunk(part);
    }
  }
  if (buffer.trim()) processChunk(buffer);

  if (runError && !reply) {
    throw new Error(runError);
  }
  return reply.trim() || "Main Agent 已完成窗簾指令。";
}

export default function CurtainControl({
  initialStatus,
  initialSchedules,
  submit,
  busy: externalBusy = false,
  onRefresh,
}: CurtainControlProps = {}) {
  const [status, setStatus] = useState<CurtainState | null>(initialStatus ?? null);
  const [sliderValue, setSliderValue] = useState(initialStatus?.target ?? 0);
  const editingSlider = useRef(false);
  const [command, setCommand] = useState("");
  const [message, setMessage] = useState("你好！指令會經由 Main Agent 委派 Curtain Agent 執行並記錄操作日誌。");
  const [connected, setConnected] = useState(!!initialStatus);
  const [localBusy, setLocalBusy] = useState(false);

  const [scheduleTime, setScheduleTime] = useState("07:00");
  const [schedulePosition, setSchedulePosition] = useState(100);
  const [schedules, setSchedules] = useState<CurtainSchedule[]>(initialSchedules ?? []);

  const busy = externalBusy || localBusy;

  useEffect(() => {
    if (!initialStatus) return;
    setStatus(initialStatus);
    if (!editingSlider.current) setSliderValue(initialStatus.target);
    setConnected(true);
  }, [initialStatus]);

  useEffect(() => {
    if (initialSchedules) {
      setSchedules(initialSchedules);
    }
  }, [initialSchedules]);

  const loadStatus = useCallback(async () => {
    try {
      const result = await apiRequest<CurtainState>("/status");
      setStatus(result);
      if (!editingSlider.current) setSliderValue(result.target);
      setConnected(true);
    } catch {
      setConnected(false);
      setMessage("無法連接 Python 後端，請確認窗簾服務是否啟動");
    }
  }, []);

  const loadSchedules = useCallback(async () => {
    try {
      const result = await apiRequest<CurtainSchedule[]>("/schedules");
      setSchedules(result);
    } catch {
      // 後端暫時無法連線時，保留目前排程畫面。
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    if (!initialSchedules) {
      void loadSchedules();
    }
  }, [loadStatus, loadSchedules, initialSchedules]);

  // Only poll while the curtain is actively moving; zero polling when stopped.
  useEffect(() => {
    if (status?.status !== "moving") return;
    const timer = window.setInterval(() => {
      void loadStatus();
    }, 400);
    return () => window.clearInterval(timer);
  }, [status?.status, loadStatus]);

  const moveTo = async (nextPosition: number) => {
    if (submit) {
      setSliderValue(nextPosition);
      submit("curtain_position", {
        device_id: "curtain-1",
        position: nextPosition,
        duration_seconds: 3,
      });
      setMessage(`已提交窗簾移動至 ${nextPosition}%（等待控制服務處理）`);
      return;
    }
    setLocalBusy(true);
    try {
      const result = await apiRequest<CurtainState>("/position", "POST", {
        position: nextPosition,
        duration_seconds: 3,
      });
      setStatus(result);
      setSliderValue(nextPosition);
      setMessage(`已送出指令：窗簾移動至 ${nextPosition}%`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "控制失敗");
    } finally {
      setLocalBusy(false);
    }
  };

  const stopCurtain = async () => {
    if (submit) {
      editingSlider.current = false;
      submit("curtain_stop", { device_id: "curtain-1" });
      setMessage("已提交停止窗簾指令（等待控制服務處理）");
      return;
    }
    setLocalBusy(true);
    try {
      const result = await apiRequest<CurtainState>("/stop", "POST");
      setStatus(result);
      setSliderValue(result.position);
      editingSlider.current = false;
      setMessage(`窗簾已停止，目前位置 ${Math.round(result.position)}%`);
    } catch {
      setMessage("停止指令失敗");
    } finally {
      setLocalBusy(false);
    }
  };

  const selectScene = async (scene: "open" | "half" | "close") => {
    const names = { open: "全開", half: "半開", close: "全關" };
    if (submit) {
      editingSlider.current = false;
      submit("curtain_scene", { device_id: "curtain-1", scene });
      setMessage(`已提交場景：${names[scene]}（等待控制服務處理）`);
      return;
    }
    setLocalBusy(true);
    try {
      const result = await apiRequest<CurtainState>("/scene", "POST", { scene });
      setStatus(result);
      editingSlider.current = false;
      setSliderValue(result.target);
      setMessage(`場景已執行：${names[scene]}`);
    } catch {
      setMessage("場景執行失敗");
    } finally {
      setLocalBusy(false);
    }
  };

  const executeCommand = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = command.trim();
    if (!trimmed) return;

    setLocalBusy(true);
    setMessage("正在送交 Main Agent 處理窗簾指令…");
    try {
      const reply = await runViaMainAgent(trimmed, (progressText) => {
        setMessage(progressText);
      });
      setMessage(reply);
      setCommand("");
      editingSlider.current = false;
      await Promise.all([loadStatus(), loadSchedules()]);
      onRefresh?.();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "指令執行失敗");
    } finally {
      setLocalBusy(false);
    }
  };

  const addSchedule = async () => {
    if (submit) {
      submit("curtain_schedule_create", {
        device_id: "curtain-1",
        time: scheduleTime,
        position: schedulePosition,
        enabled: true,
      });
      setMessage(`已提交新增排程：${scheduleTime} → ${schedulePosition}%`);
      return;
    }
    setLocalBusy(true);
    try {
      await apiRequest<CurtainSchedule>("/schedules", "POST", {
        time: scheduleTime,
        position: schedulePosition,
        enabled: true,
      });
      await loadSchedules();
      setMessage(`已新增排程：${scheduleTime}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "新增排程失敗");
    } finally {
      setLocalBusy(false);
    }
  };

  const toggleSchedule = async (item: CurtainSchedule) => {
    if (submit) {
      submit("curtain_schedule_toggle", {
        device_id: "curtain-1",
        schedule_id: item.id,
        enabled: !item.enabled,
      });
      setMessage(`已提交${item.enabled ? "停用" : "啟用"}排程：${item.time}`);
      return;
    }
    try {
      await apiRequest<CurtainSchedule>(`/schedules/${item.id}/toggle`, "POST");
      await loadSchedules();
    } catch {
      setMessage("排程狀態修改失敗");
    }
  };

  const deleteSchedule = async (item: CurtainSchedule) => {
    if (submit) {
      submit("curtain_schedule_delete", {
        device_id: "curtain-1",
        schedule_id: item.id,
      });
      setMessage(`已提交刪除排程：${item.time}`);
      return;
    }
    try {
      await apiRequest(`/schedules/${item.id}`, "DELETE");
      await loadSchedules();
      setMessage("排程已刪除");
    } catch {
      setMessage("刪除排程失敗");
    }
  };

  const position = status?.position ?? 0;
  const moving = status?.status === "moving";
  const targetLabel = Math.round(sliderValue);
  const currentLabel = Math.round(position);

  return (
    <div className="curtain-control">
      <div className="curtain-device-status">
        <div className="curtain-device-icon">
          <Blinds size={23} strokeWidth={1.5} />
        </div>
        <div className="curtain-device-info">
          <strong>智慧窗簾</strong>
          <span>CURTAIN AGENT · MAIN AGENT MANAGED</span>
        </div>
        <span className={`curtain-status-badge ${connected ? "online" : "offline"}`}>
          <span />
          {connected ? "已連線" : "未連線"}
        </span>
      </div>

      <section className="curtain-overview">
        <div className="curtain-overview-heading">
          <div>
            <span className="curtain-eyebrow">CURRENT POSITION</span>
            <h3>客廳窗簾</h3>
            <p>{moving ? "窗簾正在移動" : "窗簾目前已停止"}</p>
          </div>
          <div className="curtain-position-value">
            <strong>{currentLabel}</strong>
            <span>%</span>
          </div>
        </div>

        <div
          className="curtain-preview"
          role="img"
          aria-label={`窗簾目前開啟 ${currentLabel}%`}
        >
          <div className="curtain-preview-top">
            <span>LIVING ROOM / 01</span>
            <span>{moving ? "MOVING" : "STANDBY"}</span>
          </div>
          <div className="curtain-window">
            <div className="curtain-window-glass">
              <div className="curtain-window-light" />
              <div className="curtain-window-mullion" />
            </div>
            <div
              className="curtain-fabric curtain-fabric-left"
              style={{ width: `${(100 - position) / 2}%` }}
            />
            <div
              className="curtain-fabric curtain-fabric-right"
              style={{ width: `${(100 - position) / 2}%` }}
            />
            <div className="curtain-window-rail" />
          </div>
          <div className="curtain-preview-caption">
            <span>目前開啟程度</span>
            <strong>{currentLabel}%</strong>
          </div>
        </div>
      </section>

      <section className="curtain-section">
        <div className="curtain-section-title">
          <div>
            <span className="curtain-eyebrow">POSITION CONTROL</span>
            <h3>開合位置</h3>
          </div>
          <strong className="curtain-target-value">{targetLabel}%</strong>
        </div>

        <input
          className="curtain-range"
          type="range"
          min={0}
          max={100}
          step={1}
          value={sliderValue}
          disabled={!connected || busy}
          aria-label="窗簾目標開啟程度"
          onChange={(event) => {
            editingSlider.current = true;
            setSliderValue(Number(event.target.value));
          }}
        />
        <div className="curtain-range-labels">
          <span>全關 0%</span>
          <span>半開 50%</span>
          <span>全開 100%</span>
        </div>

        <button
          type="button"
          className="curtain-primary-button curtain-apply-button"
          disabled={!connected || busy}
          onClick={() => {
            editingSlider.current = false;
            void moveTo(sliderValue);
          }}
        >
          <CheckCircle2 size={16} />
          {busy ? "處理中…" : "移動到指定位置"}
        </button>

        <div className="curtain-scene-grid">
          <button type="button" disabled={!connected || busy} onClick={() => void selectScene("open")}>
            全開
            <small>100%</small>
          </button>
          <button type="button" disabled={!connected || busy} onClick={() => void selectScene("half")}>
            半開
            <small>50%</small>
          </button>
          <button type="button" disabled={!connected || busy} onClick={() => void selectScene("close")}>
            全關
            <small>0%</small>
          </button>
          <button type="button" className="curtain-stop-button" disabled={!connected || busy} onClick={() => void stopCurtain()}>
            停止
          </button>
        </div>
      </section>

      <section className="curtain-agent">
        <div className="curtain-agent-heading">
          <div className="curtain-agent-icon">
            <Bot size={21} />
          </div>
          <div>
            <strong>Curtain Agent</strong>
            <span>VIA MAIN AGENT · MCP</span>
          </div>
          <span className={`curtain-agent-online ${connected ? "" : "offline"}`}>
            {connected ? "MAIN AGENT" : "OFFLINE"}
          </span>
        </div>

        <div className="curtain-agent-message" role="status" aria-live="polite">
          <Bot size={16} />
          <p>{message}</p>
        </div>

        <form className="curtain-agent-form" onSubmit={executeCommand}>
          <input
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            placeholder="輸入指令，例如：把窗簾打開一半、停用 07:00 排程"
            aria-label="輸入窗簾中文指令"
            disabled={!connected || busy}
          />
          <button type="submit" aria-label="送出窗簾指令" disabled={!connected || busy || !command.trim()}>
            <Send size={17} />
          </button>
        </form>

        <div className="curtain-quick-commands">
          {["全開", "半開", "全關", "停止", "停用 07:00 排程", "刪除 07:00 排程"].map((item) => (
            <button
              type="button"
              key={item}
              disabled={!connected || busy}
              onClick={() => setCommand(item)}
            >
              {item}
            </button>
          ))}
        </div>
        <p className="curtain-agent-note">
          經由 Main Agent 委派 Curtain Agent（MCP）執行，所有操作與排程異動皆記錄於系統診斷日誌。
        </p>
      </section>

      <section className="curtain-schedule">
        <div className="curtain-schedule-heading">
          <div className="curtain-schedule-icon">
            <Clock3 size={18} />
          </div>
          <div>
            <span className="curtain-eyebrow">AUTOMATION · LOGICAL CLOCK</span>
            <h3>自動排程設定</h3>
          </div>
        </div>

        <div className="curtain-schedule-form">
          <label>
            <span>執行時間</span>
            <input
              type="time"
              value={scheduleTime}
              onChange={(event) => setScheduleTime(event.target.value)}
              disabled={!connected || busy}
            />
          </label>
          <label>
            <span>目標位置</span>
            <select
              value={schedulePosition}
              onChange={(event) => setSchedulePosition(Number(event.target.value))}
              disabled={!connected || busy}
            >
              <option value={100}>全開 100%</option>
              <option value={50}>半開 50%</option>
              <option value={0}>全關 0%</option>
            </select>
          </label>
          <button
            type="button"
            className="curtain-primary-button"
            disabled={!connected || busy}
            onClick={() => void addSchedule()}
          >
            新增排程
          </button>
        </div>

        {schedules.length > 0 ? (
          <div className="curtain-schedule-list">
            {schedules.map((item) => (
              <div className="curtain-schedule-row" key={item.id}>
                <label className="curtain-schedule-toggle">
                  <input
                    type="checkbox"
                    checked={item.enabled}
                    onChange={() => void toggleSchedule(item)}
                    disabled={!connected || busy}
                  />
                  <span>{item.time}</span>
                  <span className="curtain-schedule-arrow">→</span>
                  <strong>{item.position}%</strong>
                </label>
                <button
                  type="button"
                  className="curtain-delete-button"
                  onClick={() => void deleteSchedule(item)}
                  disabled={!connected || busy}
                >
                  刪除
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="curtain-empty-schedule">尚未設定排程</p>
        )}
      </section>
    </div>
  );
}

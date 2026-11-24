import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  Blinds,
  Bot,
  CheckCircle2,
  Clock3,
  Send,
} from "lucide-react";
import "./curtain.css";

const API = "/api/curtain";

interface CurtainStatus {
  device_id: string;
  position: number;
  target: number;
  status: "moving" | "stopped";
  updated_at: string;
  duration_seconds?: number;
}

interface CurtainSchedule {
  id: string;
  time: string;
  position: number;
  enabled: boolean;
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

export default function CurtainControl({
  initialStatus,
}: {
  initialStatus?: CurtainStatus | null;
} = {}) {
  const [status, setStatus] = useState<CurtainStatus | null>(initialStatus ?? null);
  const [sliderValue, setSliderValue] = useState(initialStatus?.target ?? 0);
  const editingSlider = useRef(false);
  const [command, setCommand] = useState("");
  const [message, setMessage] = useState("正在連接窗簾設備...");
  const [connected, setConnected] = useState(!!initialStatus);
  const [busy, setBusy] = useState(false);

  const [scheduleTime, setScheduleTime] = useState("07:00");
  const [schedulePosition, setSchedulePosition] = useState(100);
  const [schedules, setSchedules] = useState<CurtainSchedule[]>([]);

  useEffect(() => {
    if (!initialStatus) return;
    setStatus(initialStatus);
    if (!editingSlider.current) setSliderValue(initialStatus.target);
    setConnected(true);
  }, [initialStatus]);

  const loadStatus = useCallback(async () => {
    try {
      const result = await apiRequest<CurtainStatus>("/status");
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
    void loadSchedules();
  }, [loadStatus, loadSchedules]);

  // Only poll while the curtain is actively moving; zero polling when stopped.
  useEffect(() => {
    if (status?.status !== "moving") return;
    const timer = window.setInterval(() => {
      void loadStatus();
    }, 400);
    return () => window.clearInterval(timer);
  }, [status?.status, loadStatus]);

  const moveTo = async (nextPosition: number) => {
    setBusy(true);
    try {
      const result = await apiRequest<CurtainStatus>("/position", "POST", {
        position: nextPosition,
        duration_seconds: 3,
      });
      setStatus(result);
      setSliderValue(nextPosition);
      setMessage(`已送出指令：窗簾移動至 ${nextPosition}%`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "控制失敗");
    } finally {
      setBusy(false);
    }
  };

  const stopCurtain = async () => {
    setBusy(true);
    try {
      const result = await apiRequest<CurtainStatus>("/stop", "POST");
      setStatus(result);
      setSliderValue(result.position);
      editingSlider.current = false;
      setMessage(`窗簾已停止，目前位置 ${Math.round(result.position)}%`);
    } catch {
      setMessage("停止指令失敗");
    } finally {
      setBusy(false);
    }
  };

  const selectScene = async (scene: "open" | "half" | "close") => {
    const names = { open: "全開", half: "半開", close: "全關" };
    setBusy(true);
    try {
      const result = await apiRequest<CurtainStatus>("/scene", "POST", { scene });
      setStatus(result);
      editingSlider.current = false;
      setSliderValue(result.target);
      setMessage(`場景已執行：${names[scene]}`);
    } catch {
      setMessage("場景執行失敗");
    } finally {
      setBusy(false);
    }
  };

  const executeCommand = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!command.trim()) return;

    setBusy(true);
    try {
      const result = await apiRequest<CurtainStatus>("/command", "POST", {
        command: command.trim(),
      });
      setStatus(result);
      editingSlider.current = false;
      setSliderValue(result.target);
      setMessage(`指令執行成功：${command.trim()}`);
      setCommand("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "指令執行失敗");
    } finally {
      setBusy(false);
    }
  };

  const addSchedule = async () => {
    setBusy(true);
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
      setBusy(false);
    }
  };

  const toggleSchedule = async (id: string) => {
    try {
      await apiRequest<CurtainSchedule>(`/schedules/${id}/toggle`, "POST");
      await loadSchedules();
    } catch {
      setMessage("排程狀態修改失敗");
    }
  };

  const deleteSchedule = async (id: string) => {
    try {
      await apiRequest(`/schedules/${id}`, "DELETE");
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
          <span>CURTAIN AGENT · VIRTUAL DEVICE</span>
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
            <span>SMART CURTAIN ASSISTANT</span>
          </div>
          <span className={`curtain-agent-online ${connected ? "" : "offline"}`}>
            {connected ? "LOCAL" : "OFFLINE"}
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
            placeholder="輸入指令，例如：把窗簾打開一半"
            aria-label="輸入窗簾中文指令"
            disabled={!connected || busy}
          />
          <button type="submit" aria-label="送出窗簾指令" disabled={!connected || busy || !command.trim()}>
            <Send size={17} />
          </button>
        </form>

        <div className="curtain-quick-commands">
          {["全開", "半開", "全關", "停止"].map((item) => (
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
          支援中文窗簾指令；執行結果以本機設備服務回報為準。
        </p>
      </section>

      <section className="curtain-schedule">
        <div className="curtain-schedule-heading">
          <div className="curtain-schedule-icon">
            <Clock3 size={18} />
          </div>
          <div>
            <span className="curtain-eyebrow">AUTOMATION</span>
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
                    onChange={() => void toggleSchedule(item.id)}
                    disabled={!connected}
                  />
                  <span>{item.time}</span>
                  <span className="curtain-schedule-arrow">→</span>
                  <strong>{item.position}%</strong>
                </label>
                <button
                  type="button"
                  className="curtain-delete-button"
                  onClick={() => void deleteSchedule(item.id)}
                  disabled={!connected}
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

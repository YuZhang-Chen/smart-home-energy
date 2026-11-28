import { useEffect, useState, type FormEvent } from "react";
import {
  Tv,
  Power,
  Volume2,
  VolumeX,
  Monitor,
  Play,
  Pause,
  Square,
  Bot,
  Send,
  CheckCircle2,
} from "lucide-react";

import type { TVState } from "./types";

type InputSource = "HDMI1" | "HDMI2" | "HDMI3" | "HDMI4";
type Playback = "playing" | "paused" | "stopped";
type SubmitFn = (kind: string, payload: Record<string, unknown>) => void;

interface TVControlProps {
  power: boolean;
  onPowerChange: (power: boolean) => void;
  tvState?: TVState | null;
  submit?: SubmitFn;
  busy?: boolean;
  onRefresh?: () => void;
}

async function runViaMainAgent(
  text: string,
  onProgress: (msg: string) => void,
): Promise<string> {
  const runId = `tv-${crypto.randomUUID()}`;
  const messageId = `msg-${runId}`;
  const content = /電視|tv|hdmi/i.test(text) ? text : `客廳電視：${text}`;

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
  return reply.trim() || "Main Agent 已完成電視指令。";
}

export default function TVControl({
  power,
  onPowerChange,
  tvState,
  submit,
  busy: externalBusy = false,
  onRefresh,
}: TVControlProps) {
  const [volume, setVolume] = useState(25);
  const [muted, setMuted] = useState(false);
  const [input, setInput] = useState<InputSource>("HDMI1");
  const [playback, setPlayback] = useState<Playback>("stopped");
  const [command, setCommand] = useState("");
  const [localBusy, setLocalBusy] = useState(false);
  const [message, setMessage] = useState(
    "你好！指令會經由 Main Agent 委派 TV Agent 執行並記錄操作日誌。",
  );
  const [history, setHistory] = useState<string[]>([]);

  const busy = externalBusy || localBusy;

  const notify = (text: string) => {
    setMessage(text);
    setHistory((old) => [text, ...old].slice(0, 4));
  };

  const dispatchControl = (payload: Record<string, unknown>) => {
    if (submit) {
      submit("tv_control", { device_id: "tv-1", ...payload });
      return;
    }
    void fetch("/api/tv/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(async (res) => {
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(typeof data?.detail === "string" ? data.detail : "電視控制請求失敗");
        }
        onRefresh?.();
      })
      .catch((error: unknown) => {
        notify(error instanceof Error ? error.message : "無法連線至電視控制服務");
      });
  };

  useEffect(() => {
    if (!tvState) return;
    if (typeof tvState.power === "boolean") onPowerChange(tvState.power);
    if (typeof tvState.volume === "number") setVolume(tvState.volume);
    if (typeof tvState.muted === "boolean") setMuted(tvState.muted);
    if (typeof tvState.input === "string") setInput(tvState.input);
    if (typeof tvState.playback === "string") setPlayback(tvState.playback);
  }, [tvState, onPowerChange]);

  const togglePower = () => {
    if (power) {
      onPowerChange(false);
      setPlayback("stopped");
      dispatchControl({ power: false, playback: "stopped" });
      notify("電視已關閉，進入待機狀態。");
    } else {
      onPowerChange(true);
      dispatchControl({ power: true });
      notify("電視已開啟，歡迎回家！");
    }
  };

  const changeVolume = (value: number) => {
    const next = Math.max(0, Math.min(100, value));
    setVolume(next);
    setMuted(false);
    dispatchControl({ volume: next, muted: false });
  };

  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    dispatchControl({ muted: next });
    notify(next ? "已開啟靜音。" : "已解除靜音。");
  };

  const changeInput = (source: InputSource) => {
    if (!power) {
      notify("請先開啟電視，再切換訊號來源。");
      return;
    }
    setInput(source);
    dispatchControl({ input: source });
    notify(`已切換至 ${source}。`);
  };

  const changePlayback = (value: Playback) => {
    if (!power) {
      notify("請先開啟電視。");
      return;
    }
    setPlayback(value);
    dispatchControl({ playback: value });

    const names = {
      playing: "開始播放",
      paused: "已暫停播放",
      stopped: "已停止播放",
    };

    notify(names[value] + "。");
  };

  const executeCommand = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = command.trim();
    if (!text || busy) return;

    setLocalBusy(true);
    setCommand("");
    setMessage("正在透過 Main Agent 委派 TV Agent 處理…");
    try {
      const reply = await runViaMainAgent(text, (progress) => setMessage(progress));
      notify(reply);
      onRefresh?.();
    } catch (error: unknown) {
      notify(error instanceof Error ? error.message : "無法連線至 Main Agent");
    } finally {
      setLocalBusy(false);
    }
  };

  return (
    <div className="tv-control">
      {/* 狀態列 */}
      <div className={`tv-status-strip ${power ? "is-on" : ""}`}>
        <CheckCircle2 size={15} />
        <span>
          {power
            ? `運作中 · ${input} · 音量 ${muted ? "靜音" : `${volume}%`}`
            : "待機中 · 尚未開啟電視"}
        </span>
      </div>

      {/* 電源 */}
      <section className="tv-section">
        <div className="tv-section-title">
          <div>
            <span className="tv-eyebrow">POWER</span>
            <h3>電視電源</h3>
          </div>
          <span className={`tv-badge ${power ? "is-on" : ""}`}>
            {power ? "開啟" : "關閉"}
          </span>
        </div>

        <button
          type="button"
          disabled={busy}
          className={`tv-power-button ${power ? "is-on" : ""}`}
          onClick={togglePower}
        >
          <Power size={18} />
          {power ? "關閉電視" : "開啟電視"}
        </button>
      </section>

      {/* 音量 */}
      <section className="tv-section">
        <div className="tv-section-title">
          <div>
            <span className="tv-eyebrow">VOLUME</span>
            <h3>音量控制</h3>
          </div>
          <strong className="tv-volume-value">
            {muted ? "MUTE" : `${volume}%`}
          </strong>
        </div>

        <div className="tv-volume-control">
          <button
            type="button"
            disabled={!power || busy}
            aria-label="切換靜音"
            onClick={toggleMute}
          >
            {muted ? <VolumeX /> : <Volume2 />}
          </button>

          <input
            type="range"
            min="0"
            max="100"
            value={volume}
            disabled={!power || busy}
            aria-label="電視音量"
            onChange={(event) => changeVolume(Number(event.target.value))}
          />

          <button
            type="button"
            disabled={!power || busy}
            aria-label="增加音量"
            onClick={() => changeVolume(volume + 5)}
          >
            +
          </button>
        </div>

        <div className="tv-volume-labels">
          <span>0</span>
          <span>50</span>
          <span>100</span>
        </div>
      </section>

      {/* HDMI */}
      <section className="tv-section">
        <div className="tv-section-title">
          <div>
            <span className="tv-eyebrow">INPUT SOURCE</span>
            <h3>訊號來源</h3>
          </div>
          <span className="tv-current-input">{input}</span>
        </div>

        <div className="tv-input-grid">
          {(["HDMI1", "HDMI2", "HDMI3", "HDMI4"] as const).map(
            (source) => (
              <button
                key={source}
                type="button"
                disabled={!power || busy}
                className={input === source ? "selected" : ""}
                onClick={() => changeInput(source)}
              >
                <Monitor size={15} />
                {source}
              </button>
            ),
          )}
        </div>
      </section>

      {/* 播放 */}
      <section className="tv-section">
        <div className="tv-section-title">
          <div>
            <span className="tv-eyebrow">MEDIA CONTROL</span>
            <h3>播放控制</h3>
          </div>
          <span className="tv-current-input">
            {playback === "playing"
              ? "播放中"
              : playback === "paused"
                ? "已暫停"
                : "已停止"}
          </span>
        </div>

        <div className="tv-playback-controls">
          <button
            type="button"
            disabled={!power || busy}
            className={playback === "playing" ? "selected" : ""}
            onClick={() => changePlayback("playing")}
            aria-label="播放"
          >
            <Play size={21} />
          </button>

          <button
            type="button"
            disabled={!power || busy}
            className={playback === "paused" ? "selected" : ""}
            onClick={() => changePlayback("paused")}
            aria-label="暫停"
          >
            <Pause size={21} />
          </button>

          <button
            type="button"
            disabled={!power || busy}
            className={playback === "stopped" ? "selected" : ""}
            onClick={() => changePlayback("stopped")}
            aria-label="停止"
          >
            <Square size={19} />
          </button>
        </div>
      </section>

      {/* AI Agent */}
      <section className="tv-agent">
        <div className="tv-agent-heading">
          <div className="tv-agent-icon">
            <Bot size={22} />
          </div>
          <div>
            <strong>TV Agent</strong>
            <span>MAIN AGENT · MCP</span>
          </div>
          <span className="tv-agent-online">ONLINE</span>
        </div>

        <div className="tv-agent-message">
          <Bot size={17} />
          <p>{message}</p>
        </div>

        <form className="tv-agent-form" onSubmit={executeCommand}>
          <input
            value={command}
            disabled={busy}
            onChange={(event) => setCommand(event.target.value)}
            placeholder="輸入指令，例如：音量調到 30 或太吵了"
            aria-label="輸入電視指令"
          />
          <button type="submit" disabled={busy || !command.trim()} aria-label="送出指令">
            <Send size={18} />
          </button>
        </form>

        <div className="tv-quick-commands">
          {["打開電視", "音量調到 30", "切換 HDMI 2", "幫我靜音"].map(
            (item) => (
              <button
                type="button"
                key={item}
                disabled={busy}
                onClick={() => setCommand(item)}
              >
                {item}
              </button>
            ),
          )}
        </div>

        {history.length > 0 && (
          <div className="tv-history">
            <span>最近操作</span>
            {history.map((item, index) => (
              <p key={`${item}-${index}`}>{item}</p>
            ))}
          </div>
        )}

        <p className="tv-agent-note">
          手動操作透過持久化請求佇列由 TVController 寫入操作日誌；自然語言指令經由 Main Agent 委派 TV Agent（MCP）執行。
        </p>
      </section>

      <div className="tv-footer-note">
        <Tv size={14} />
        <span>LG Smart TV</span>
      </div>
    </div>
  );
}

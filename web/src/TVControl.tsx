
import { useState, type FormEvent } from "react";
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


type InputSource = "HDMI1" | "HDMI2" | "HDMI3" | "HDMI4";
type Playback = "playing" | "paused" | "stopped";

interface TVControlProps {
  power: boolean;
  onPowerChange: (power: boolean) => void;
}

export default function TVControl({
  power,
  onPowerChange,
}: TVControlProps) {
  const [volume, setVolume] = useState(25);
  const [muted, setMuted] = useState(false);
  const [input, setInput] = useState<InputSource>("HDMI1");
  const [playback, setPlayback] = useState<Playback>("stopped");
  const [command, setCommand] = useState("");
  const [message, setMessage] = useState(
    "你好！我是 TV Agent，可以幫你控制智慧電視。"
  );
  const [history, setHistory] = useState<string[]>([]);


  const notify = (text: string) => {
    setMessage(text);
    setHistory((old) => [text, ...old].slice(0, 4));
  };

const togglePower = () => {
  if (power) {
    onPowerChange(false);
    setPlayback("stopped");
    notify("電視已關閉，進入待機狀態。");
  } else {
    onPowerChange(true);
    notify("電視已開啟，歡迎回家！");
  }
};

  const changeVolume = (value: number) => {
    const next = Math.max(0, Math.min(100, value));
    setVolume(next);
    setMuted(false);
  };

  const changeInput = (source: InputSource) => {
    if (!power) {
      notify("請先開啟電視，再切換訊號來源。");
      return;
    }

    setInput(source);
    notify(`已切換至 ${source}。`);
  };

  const changePlayback = (value: Playback) => {
    if (!power) {
      notify("請先開啟電視。");
      return;
    }

    setPlayback(value);

    const names = {
      playing: "開始播放",
      paused: "已暫停播放",
      stopped: "已停止播放",
    };

    notify(names[value] + "。");
  };

  const executeCommand = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const text = command.trim();

    if (!text) return;

    const normalized = text.toLowerCase();
    const numberMatch = normalized.match(/\d+/);
    const number = numberMatch ? Number(numberMatch[0]) : null;

    if (/關閉電視|關掉電視|電視關機|關機/.test(normalized)) {
      onPowerChange(false);
      setPlayback("stopped");
      notify("AI 已執行：關閉電視。");
    } else if (/打開電視|開啟電視|電視開機|開機/.test(normalized)) {
      onPowerChange(true);
      notify("AI 已執行：開啟電視。");
    } else if (/取消靜音|解除靜音/.test(normalized)) {
      setMuted(false);
      notify("AI 已執行：解除靜音。");
    } else if (/靜音/.test(normalized)) {
      setMuted(true);
      notify("AI 已執行：開啟靜音。");
    } else if (/音量/.test(normalized) && number !== null) {
      if (number < 0 || number > 100) {
        notify("音量範圍為 0～100。");
      } else if (!power) {
        notify("請先開啟電視，再調整音量。");
      } else {
        changeVolume(number);
        notify(`AI 已將音量調整至 ${number}。`);
      }
    
} else if (/hdmi/i.test(normalized) || /切換.*[1-4]/.test(normalized)) {
  const match = normalized.match(/hdmi\s*([1-4])/i);

  if (!match) {
    notify("請指定 HDMI 1～4，例如：切換到 HDMI 2。");
  } else {
    changeInput(`HDMI${match[1]}` as InputSource);
  }

    } else if (/暫停/.test(normalized)) {
      changePlayback("paused");
    } else if (/播放|繼續/.test(normalized)) {
      changePlayback("playing");
    } else if (/停止/.test(normalized)) {
      changePlayback("stopped");
    } else {
      notify("尚未辨識這個指令，請換個方式描述。");
    }

    setCommand("");
  };

  return (
    <div className="tv-control">

      {/* 電視設備資訊 */}
      <div className="tv-device-status">
        <div className="tv-device-icon">
          <Tv size={25} />
        </div>

        <div className="tv-device-info">
          <strong>LG Smart TV</strong>
          <span>LG webOS 26 · Virtual Device</span>
        </div>

        <span className={`tv-status-badge ${power ? "on" : "off"}`}>
          <span />
          {power ? "運作中" : "待機中"}
        </span>
      </div>

      {/* 電視螢幕 */}
      <div className={`tv-screen ${power ? "active" : ""}`}>
        {power ? (
          <>
            <div className="tv-screen-brand">LG webOS</div>
            <Monitor size={43} strokeWidth={1.2} />
            <strong>智慧家庭娛樂中心</strong>
            <span>{input} · {playback === "playing" ? "播放中" : playback === "paused" ? "已暫停" : "待播放"}</span>
            <div className="tv-screen-apps">
              <span>Netflix</span>
              <span>YouTube</span>
              <span>Disney+</span>
            </div>
          </>
        ) : (
          <>
            <Tv size={45} strokeWidth={1.2} />
            <strong>電視目前已關閉</strong>
            <span>按下電源按鈕開啟電視</span>
          </>
        )}
      </div>

      {/* 電源 */}
      <section className="tv-section">
        <div className="tv-section-title">
          <div>
            <span className="tv-eyebrow">POWER</span>
            <h3>電源控制</h3>
          </div>
          <button
            type="button"
            className={`tv-power-button ${power ? "active" : ""}`}
            onClick={togglePower}
          >
            <Power size={18} />
            {power ? "關閉電視" : "開啟電視"}
          </button>
        </div>
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
            disabled={!power}
            aria-label="切換靜音"
            onClick={() => setMuted(!muted)}
          >
            {muted ? <VolumeX /> : <Volume2 />}
          </button>

          <input
            type="range"
            min="0"
            max="100"
            value={volume}
            disabled={!power}
            aria-label="電視音量"
            onChange={(event) => changeVolume(Number(event.target.value))}
          />

          <button
            type="button"
            disabled={!power}
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
                disabled={!power}
                className={input === source ? "selected" : ""}
                onClick={() => changeInput(source)}
              >
                <Monitor size={17} />
                {source}
                {input === source && <CheckCircle2 size={15} />}
              </button>
            )
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
            disabled={!power}
            className={playback === "playing" ? "selected" : ""}
            onClick={() => changePlayback("playing")}
            aria-label="播放"
          >
            <Play size={21} />
          </button>

          <button
            type="button"
            disabled={!power}
            className={playback === "paused" ? "selected" : ""}
            onClick={() => changePlayback("paused")}
            aria-label="暫停"
          >
            <Pause size={21} />
          </button>

          <button
            type="button"
            disabled={!power}
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
            <span>SMART TV ASSISTANT</span>
          </div>
          <span className="tv-agent-online">LOCAL</span>
        </div>

        <div className="tv-agent-message">
          <Bot size={17} />
          <p>{message}</p>
        </div>

        <form className="tv-agent-form" onSubmit={executeCommand}>
          <input
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            placeholder="輸入指令，例如：音量調到 30"
            aria-label="輸入電視指令"
          />
          <button type="submit" aria-label="送出指令">
            <Send size={18} />
          </button>
        </form>

        <div className="tv-quick-commands">
          {["打開電視", "音量調到 30", "切換 HDMI 2", "幫我靜音"].map(
            (item) => (
              <button
                type="button"
                key={item}
                onClick={() => setCommand(item)}
              >
                {item}
              </button>
            )
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
          目前使用本機指令解析，尚未連接雲端 AI 或真實電視。
        </p>
      </section>

    </div>
  );
}

import { Clock3, Moon, Pause, Play, RotateCcw, Sun } from "lucide-react";
import { formatTime, phaseForMinute, type TimeMode } from "./dayNight";

export default function DayNightControls({
  mode,
  minute,
  playing,
  onMode,
  onMinute,
  onToggle,
  onReplay,
}: {
  mode: TimeMode;
  minute: number;
  playing: boolean;
  onMode: (mode: TimeMode) => void;
  onMinute: (minute: number) => void;
  onToggle: () => void;
  onReplay: () => void;
}) {
  return (
    <section className="day-night-card" aria-label="晝夜循環控制">
      <div className="day-night-heading">
        <div>
          <span className="day-night-kicker">ROOM LIGHT</span>
          <h2>客廳時光</h2>
        </div>
        <div className="day-night-clock" aria-live="polite">
          <b>{formatTime(minute)}</b>
          <span>{phaseForMinute(minute)}</span>
        </div>
      </div>
      <div className="time-mode-switch" role="group" aria-label="時間模式">
        <button
          type="button"
          className={mode === "real" ? "active" : ""}
          aria-pressed={mode === "real"}
          onClick={() => onMode("real")}
        >
          <Clock3 size={15} /> 真實時間
        </button>
        <button
          type="button"
          className={mode === "manual" ? "active" : ""}
          aria-pressed={mode === "manual"}
          onClick={() => onMode("manual")}
        >
          <Sun size={15} /> 手動時間
        </button>
        <button
          type="button"
          className={mode === "simulation" ? "active" : ""}
          aria-pressed={mode === "simulation"}
          onClick={() => onMode("simulation")}
        >
          <Moon size={15} /> 加速模擬
        </button>
      </div>
      <label className="time-slider-label" htmlFor="day-night-time">
        <span>{mode === "real" ? "依本機時間同步" : mode === "simulation" ? `120 秒循環 24 小時${playing ? " · 播放中" : " · 暫停可拖曳"}` : "拖曳或使用方向鍵選擇時間"}</span>
        <span>{formatTime(minute)}</span>
      </label>
      <input
        id="day-night-time"
        aria-label="客廳時間"
        type="range"
        min={0}
        max={1439}
        step={1}
        value={Math.floor(minute)}
        disabled={mode === "real" || (mode === "simulation" && playing)}
        onChange={(event) => onMinute(Number(event.currentTarget.value))}
      />
      <div className="time-slider-scale" aria-hidden="true">
        <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>23:59</span>
      </div>
      {mode === "simulation" && (
        <div className="simulation-actions">
          <button type="button" onClick={onToggle}>
            {playing ? <Pause size={15} /> : <Play size={15} />}
            {playing ? "暫停" : "繼續播放"}
          </button>
          <button type="button" onClick={onReplay}>
            <RotateCcw size={15} /> 從 00:00 重播
          </button>
        </div>
      )}
      <p className="day-night-note">自然日照會隨時間變化；智慧燈泡維持獨立手動控制。</p>
    </section>
  );
}

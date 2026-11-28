import { useEffect, useRef, useState } from "react";
import { useFrame } from "@react-three/fiber";
import { Html } from "@react-three/drei";
import type { TVState } from "./types";

type InputSource = "HDMI1" | "HDMI2" | "HDMI3" | "HDMI4";

interface ChannelConfig {
  label: string;
  videoId: string;
  isLive?: boolean;
  screen: string;
  glow: string;
}

export const TV_CHANNELS: Record<InputSource, ChannelConfig> = {
  HDMI1: {
    label: "公視 13 台 Live",
    videoId: "C6gYqSHLRw4",
    isLive: true,
    screen: "#1d3b53",
    glow: "#38bdf8",
  },
  HDMI2: {
    label: "即時新聞 24H",
    videoId: "vr3XyVCR4T0",
    isLive: true,
    screen: "#2d1f4e",
    glow: "#818cf8",
  },
  HDMI3: {
    label: "4K 自然風景",
    videoId: "LXb3EKWsInQ",
    screen: "#1b4332",
    glow: "#34d399",
  },
  HDMI4: {
    label: "Lo-Fi 音樂頻道",
    videoId: "jfKfPfyJRdk",
    isLive: true,
    screen: "#4a2511",
    glow: "#f97316",
  },
};

function sendYouTubeCommand(
  iframe: HTMLIFrameElement | null,
  func: string,
  args: unknown[] = [],
) {
  if (!iframe?.contentWindow) return;
  try {
    iframe.contentWindow.postMessage(
      JSON.stringify({ event: "command", func, args }),
      "*",
    );
  } catch {
    // 忽略跨域或尚未就緒時的 postMessage 例外
  }
}

export default function TVScreen({ tvState }: { tvState?: TVState | null }) {
  const tvOn = tvState?.power ?? false;
  const input: InputSource =
    tvState?.input && tvState.input in TV_CHANNELS ? tvState.input : "HDMI1";
  const playback = tvState?.playback ?? "stopped";
  const volume = tvState?.volume ?? 25;
  const muted = tvState?.muted ?? false;
  const channel = TV_CHANNELS[input];

  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [frontFacing, setFrontFacing] = useState(true);
  const frontRef = useRef(true);

  // 當攝影機轉到電視後牆背面時隱藏 HTML 螢幕，避免從背面穿幫
  useFrame(({ camera }) => {
    const next = camera.position.z > -2.65;
    if (next !== frontRef.current) {
      frontRef.current = next;
      setFrontFacing(next);
    }
  });

  // 透過 YouTube IFrame postMessage API 同步播放／暫停／音量／靜音狀態
  useEffect(() => {
    if (!tvOn) return;
    const iframe = iframeRef.current;
    const sync = () => {
      sendYouTubeCommand(iframe, "setVolume", [volume]);
      sendYouTubeCommand(iframe, muted || volume === 0 ? "mute" : "unMute");
      if (playback === "paused") {
        sendYouTubeCommand(iframe, "pauseVideo");
      } else {
        sendYouTubeCommand(iframe, "playVideo");
      }
    };
    sync();
    // 剛切台或開機時 iframe 可能還在初始化，延遲再補同步一次
    const timer = window.setTimeout(sync, 900);
    return () => window.clearTimeout(timer);
  }, [tvOn, input, playback, volume, muted]);

  const originParam =
    typeof window !== "undefined" && window.location.origin
      ? `&origin=${encodeURIComponent(window.location.origin)}`
      : "";
  const loopParams = channel.isLive ? "" : `&loop=1&playlist=${channel.videoId}`;
  const channelKey = channel.videoId;
  const embedUrl = `https://www.youtube.com/embed/${channel.videoId}?enablejsapi=1${originParam}&autoplay=1&mute=1${loopParams}&controls=0&modestbranding=1&rel=0&playsinline=1&iv_load_policy=3`;

  return (
    <>
      {/* 實體螢幕底板：關機時深黑待機；開機時作為載入與離線保底微光面 */}
      <mesh position={[0, 0, 0.052]}>
        <boxGeometry args={[2.05, 1.1, 0.015]} />
        <meshStandardMaterial
          color={tvOn ? channel.screen : "#181d1b"}
          emissive={tvOn ? channel.screen : "#000000"}
          emissiveIntensity={tvOn ? (playback === "paused" ? 0.55 : 0.9) : 0}
          roughness={tvOn ? 0.25 : 0.6}
        />
      </mesh>

      {tvOn ? (
        <>
          {/* 電視螢幕發出的客廳環境柔光 */}
          <pointLight
            position={[0, 0, 0.45]}
            color={channel.glow}
            intensity={playback === "paused" ? 0.55 : 1.05}
            distance={3.8}
            decay={2}
          />

          {/* 使用 @react-three/drei Html transform 精準貼合 2.05 x 1.10 3D 電視框 */}
          {frontFacing && (
            <Html
              transform
              position={[0, 0, 0.062]}
              distanceFactor={4 / 3}
              zIndexRange={[10, 1]}
              pointerEvents="none"
            >
              <div className="tv-3d-screen" aria-hidden="true">
                <iframe
                  key={channelKey}
                  ref={iframeRef}
                  className="tv-3d-iframe"
                  src={embedUrl}
                  title={`客廳電視 ${input} ${channel.label}`}
                  allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                  referrerPolicy="strict-origin-when-cross-origin"
                />

                {/* 電視右上角訊號源 OSD */}
                <div className="tv-3d-osd-top">
                  <span className="tv-3d-badge">{input}</span>
                  <span className="tv-3d-channel">{channel.label}</span>
                </div>

                {/* 暫停遮罩提示 */}
                {playback === "paused" && (
                  <div className="tv-3d-paused">已暫停</div>
                )}

                {/* 底部音量／靜音 OSD 條 */}
                <div className="tv-3d-osd-bottom">
                  <span className="tv-3d-vol-label">
                    {muted ? "靜音" : `音量 ${volume}%`}
                  </span>
                  <div className="tv-3d-vol-track">
                    <div
                      className={`tv-3d-vol-fill ${muted ? "is-muted" : ""}`}
                      style={{ width: `${muted ? 0 : volume}%` }}
                    />
                  </div>
                </div>
              </div>
            </Html>
          )}
        </>
      ) : (
        /* 關機待機紅色指示燈 */
        <mesh position={[0.92, -0.48, 0.065]}>
          <circleGeometry args={[0.025, 16]} />
          <meshBasicMaterial color="#ef4444" />
        </mesh>
      )}
    </>
  );
}

import { describe, expect, it } from "vitest";
import { encodePcmWav, encodePcmWavBuffer } from "./audioWav";

describe("encodePcmWav", () => {
  it("writes a valid 16 kHz mono 16-bit PCM WAV header and payload", () => {
    const samples = new Float32Array([0, 0.5, -0.5, 1, -1]);
    const blob = encodePcmWav(samples, 16000);
    expect(blob.type).toBe("audio/wav");

    const buffer = encodePcmWavBuffer(samples, 16000);
    const bytes = new Uint8Array(buffer);
    const view = new DataView(buffer);
    const ascii = (start: number, len: number) =>
      String.fromCharCode(...bytes.slice(start, start + len));

    expect(bytes.byteLength).toBe(44 + samples.length * 2);
    expect(ascii(0, 4)).toBe("RIFF");
    expect(ascii(8, 4)).toBe("WAVE");
    expect(ascii(12, 4)).toBe("fmt ");
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // mono
    expect(view.getUint32(24, true)).toBe(16000); // sample rate
    expect(view.getUint16(34, true)).toBe(16); // bits per sample
    expect(ascii(36, 4)).toBe("data");
    expect(view.getUint32(40, true)).toBe(samples.length * 2);
  });
});

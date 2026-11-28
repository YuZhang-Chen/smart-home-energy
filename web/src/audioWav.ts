/**
 * Convert browser MediaRecorder blobs (e.g. audio/webm;codecs=opus) into
 * 16 kHz mono 16-bit PCM WAV blobs for OpenAI Whisper / LiteLLM compatibility.
 */

export function encodePcmWavBuffer(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const numChannels = 1;
  const bitsPerSample = 16;
  const bytesPerSample = bitsPerSample / 8;
  const blockAlign = numChannels * bytesPerSample;
  const byteRate = sampleRate * blockAlign;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  const writeAscii = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) {
      view.setUint8(offset + i, value.charCodeAt(i));
    }
  };

  writeAscii(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(8, "WAVE");
  writeAscii(12, "fmt ");
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // PCM format
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);
  writeAscii(36, "data");
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }

  return buffer;
}

export function encodePcmWav(samples: Float32Array, sampleRate: number): Blob {
  return new Blob([encodePcmWavBuffer(samples, sampleRate)], { type: "audio/wav" });
}

function mixToMono(audioBuffer: AudioBuffer): Float32Array {
  const length = audioBuffer.length;
  const channels = audioBuffer.numberOfChannels;
  if (channels <= 1) {
    return audioBuffer.getChannelData(0);
  }
  const mixed = new Float32Array(length);
  for (let ch = 0; ch < channels; ch++) {
    const data = audioBuffer.getChannelData(ch);
    for (let i = 0; i < length; i++) {
      mixed[i] += data[i] / channels;
    }
  }
  return mixed;
}

export async function convertAudioBlobToWav(
  blob: Blob,
  targetSampleRate = 16000,
): Promise<Blob> {
  const baseType = (blob.type || "").split(";")[0]?.trim().toLowerCase();
  if (baseType === "audio/wav" || baseType === "audio/x-wav" || baseType === "audio/wave") {
    return blob;
  }

  const AudioCtx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioCtx) {
    return blob;
  }

  const audioContext = new AudioCtx();
  try {
    const arrayBuffer = await blob.arrayBuffer();
    const decoded = await audioContext.decodeAudioData(arrayBuffer.slice(0));

    if (typeof OfflineAudioContext !== "undefined") {
      const targetFrames = Math.max(1, Math.ceil(decoded.duration * targetSampleRate));
      const offline = new OfflineAudioContext(1, targetFrames, targetSampleRate);
      const source = offline.createBufferSource();
      source.buffer = decoded;
      source.connect(offline.destination);
      source.start(0);
      const rendered = await offline.startRendering();
      return encodePcmWav(rendered.getChannelData(0), targetSampleRate);
    }

    return encodePcmWav(mixToMono(decoded), decoded.sampleRate);
  } finally {
    await audioContext.close().catch(() => {});
  }
}

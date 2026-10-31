/** Microphone capture (MediaRecorder) and conversion to the WAV the speech-to-text endpoint requires:
 * mono, 16-bit PCM, 16 kHz. */

const TARGET_RATE = 16_000;
const MAX_RECORD_MS = 30_000;

export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeText = (offset: number, text: string): void => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

/** Decode whatever the browser recorded (webm/opus, mp4...) and resample it to mono 16 kHz WAV. */
export async function recordingToWav(recording: Blob): Promise<Blob> {
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await recording.arrayBuffer());
    const frames = Math.max(1, Math.ceil(decoded.duration * TARGET_RATE));
    const offline = new OfflineAudioContext(1, frames, TARGET_RATE);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination); // multi-channel input is down-mixed to mono by the destination
    source.start();
    const rendered = await offline.startRendering();
    return encodeWav(rendered.getChannelData(0), TARGET_RATE);
  } finally {
    void context.close();
  }
}

export class MicRecorder {
  private recorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private chunks: Blob[] = [];
  private timer: number | undefined;
  private finished: Promise<Blob> | null = null;

  get recording(): boolean {
    return this.recorder !== null && this.recorder.state === "recording";
  }

  async start(): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.chunks = [];
    const recorder = new MediaRecorder(this.stream);
    this.recorder = recorder;
    this.finished = new Promise<Blob>((resolve) => {
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size > 0) this.chunks.push(event.data);
      });
      recorder.addEventListener("stop", () => {
        resolve(new Blob(this.chunks, { type: recorder.mimeType || "audio/webm" }));
      });
    });
    recorder.start();
    this.timer = window.setTimeout(() => this.stopRecorder(), MAX_RECORD_MS);
  }

  private stopRecorder(): void {
    window.clearTimeout(this.timer);
    if (this.recorder && this.recorder.state !== "inactive") this.recorder.stop();
    this.stream?.getTracks().forEach((track) => track.stop());
  }

  async stop(): Promise<Blob> {
    this.stopRecorder();
    if (!this.finished) throw new Error("not recording");
    return this.finished;
  }
}

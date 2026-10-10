// The microphone: 16 kHz PCM16 in 100 ms packets, each with its loudness
// (spec §5.2). The worklet is a static file under public/, loaded by URL as in
// the demo, so the bundler never has to understand an AudioWorklet module.
import type { EndReason } from "../lib/types"

export const CAPTURE_WORKLET_URL = "/voice/capture-worklet.js"

const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  video: false,
}

/** Speech RMS is small; this puts a normal voice near the top of the orb's range (as in the demo). */
const LEVEL_GAIN = 9

export interface Capture {
  setMuted: (muted: boolean) => void
  stop: () => void
}

export type PacketHandler = (audio: ArrayBuffer, level: number) => void

export function callAudioSupported(): boolean {
  return (
    typeof window.AudioContext === "function" &&
    typeof window.AudioWorkletNode === "function" &&
    typeof window.WebSocket === "function" &&
    typeof navigator.mediaDevices?.getUserMedia === "function"
  )
}

export function requestMicrophone(): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS)
}

/** Why getUserMedia refused, in the call's ending vocabulary. */
export function micFailure(error: unknown): EndReason {
  const name = typeof error === "object" && error !== null && "name" in error ? String(error.name) : ""
  if (name === "NotAllowedError" || name === "SecurityError") return "mic_denied"
  if (name === "NotFoundError" || name === "OverconstrainedError") return "mic_missing"
  if (name === "NotReadableError" || name === "AbortError") return "mic_busy"
  return "error"
}

export async function startCapture(
  context: AudioContext,
  stream: MediaStream,
  onPacket: PacketHandler,
): Promise<Capture> {
  await context.audioWorklet.addModule(CAPTURE_WORKLET_URL)
  const input = context.createMediaStreamSource(stream)
  const worklet = new AudioWorkletNode(context, "pcm-capture", {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
  })
  // Chrome only pulls audio through a graph that reaches the destination; the
  // zero gain keeps the microphone out of the speakers.
  const sink = context.createGain()
  sink.gain.value = 0
  input.connect(worklet).connect(sink).connect(context.destination)
  let muted = false
  worklet.port.onmessage = ({ data }: MessageEvent<{ audio: ArrayBuffer; level: number }>) => {
    // Muted still sends frames, only silent ones: the server never learns of it.
    if (muted) new Int16Array(data.audio).fill(0)
    onPacket(data.audio, muted ? 0 : Math.min(1, data.level * LEVEL_GAIN))
  }
  return {
    setMuted(value) {
      muted = value
      for (const track of stream.getAudioTracks()) track.enabled = !value
    },
    stop() {
      worklet.port.onmessage = null
      input.disconnect()
      worklet.disconnect()
      sink.disconnect()
      for (const track of stream.getTracks()) track.stop()
    },
  }
}

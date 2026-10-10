// The model's voice: 24 kHz PCM16 chunks played back to back on one timeline,
// all dropped at once when the user cuts in (spec §5.2). Ported from the demo's
// playAudio/clearPlayback (demos/realtime-voice/app.js).

/** Lead time before the first chunk of a burst, so its start is not clipped. */
const LEAD_SECONDS = 0.025

export class PcmPlayer {
  private readonly context: BaseAudioContext
  private readonly rate: number
  private readonly onIdle: () => void
  private readonly meter: AnalyserNode
  private readonly samples: Float32Array<ArrayBuffer>
  private readonly sources = new Set<AudioBufferSourceNode>()
  private nextTime = 0

  /** `onIdle` runs when the last queued chunk finishes on its own — not after `clear()`. */
  constructor(context: BaseAudioContext, rate: number, onIdle: () => void) {
    this.context = context
    this.rate = rate
    this.onIdle = onIdle
    // Everything plays through the analyser, so its loudness is what the orb shows.
    this.meter = context.createAnalyser()
    this.meter.fftSize = 512
    this.meter.connect(context.destination)
    this.samples = new Float32Array(this.meter.fftSize)
  }

  /** Something is queued or playing. */
  get busy(): boolean {
    return this.sources.size > 0
  }

  /** Queue one chunk after everything before it. Returns its start on the context clock, or null when empty. */
  enqueue(raw: ArrayBuffer): number | null {
    const frames = Math.floor(raw.byteLength / 2)
    if (frames === 0) return null
    const pcm = new DataView(raw)
    const buffer = this.context.createBuffer(1, frames, this.rate)
    const channel = buffer.getChannelData(0)
    for (let i = 0; i < frames; i++) channel[i] = pcm.getInt16(i * 2, true) / 32768
    const source = this.context.createBufferSource()
    source.buffer = buffer
    source.connect(this.meter)
    const start = Math.max(this.context.currentTime + LEAD_SECONDS, this.nextTime)
    this.nextTime = start + buffer.duration
    this.sources.add(source)
    source.onended = () => {
      this.sources.delete(source)
      source.disconnect()
      if (this.sources.size === 0) this.onIdle()
    }
    source.start(start)
    return start
  }

  /** Stop what is playing and drop what is queued. Returns how many sources were cut. */
  clear(): number {
    const cut = this.sources.size
    for (const source of this.sources) {
      source.onended = null
      try {
        source.stop()
      } catch {
        // Already stopped; nothing to cut.
      }
      source.disconnect()
    }
    this.sources.clear()
    this.nextTime = 0
    return cut
  }

  /** Seconds of audio still to come out of the speaker. */
  remaining(): number {
    return this.busy ? Math.max(0, this.nextTime - this.context.currentTime) : 0
  }

  /** RMS of what is coming out right now, 0–1. */
  level(): number {
    if (!this.busy) return 0
    this.meter.getFloatTimeDomainData(this.samples)
    let sum = 0
    for (const sample of this.samples) sum += sample * sample
    return Math.sqrt(sum / this.samples.length)
  }

  dispose(): void {
    this.clear()
    this.meter.disconnect()
  }
}

/** Where a context-clock time falls on the performance clock. The output
 *  timestamp includes the device's own latency when the browser reports it. */
export function contextToPerformanceTime(context: AudioContext, when: number): number {
  const stamp = typeof context.getOutputTimestamp === "function" ? context.getOutputTimestamp() : null
  if (stamp?.performanceTime && stamp.contextTime !== undefined)
    return stamp.performanceTime + (when - stamp.contextTime) * 1000
  return performance.now() + (when - context.currentTime) * 1000
}

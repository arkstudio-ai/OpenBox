// Call progress tones (spec §8), synthesised so there is nothing to ship: the
// ringback while the call is being answered, and the closing tones. They go
// straight to the destination, never through the playback queue, so a
// `playback.clear` cannot cut them.

export type Tone = "ended" | "error"

interface Note {
  hz: number
  /** Offset from the tone's start, seconds. */
  at: number
  length: number
}

const VOLUME = 0.2
/** A short ramp at each edge: a hard-gated sine clicks. */
const RAMP = 0.005

const TONES: Record<Tone, Note[]> = {
  ended: [
    { hz: 660, at: 0, length: 0.09 },
    { hz: 440, at: 0.09, length: 0.09 },
  ],
  error: [{ hz: 330, at: 0, length: 0.2 }],
}

/** Play a tone now. Returns its length in seconds. */
export function playTone(context: BaseAudioContext, tone: Tone): number {
  const start = context.currentTime + 0.01
  let end = 0
  for (const note of TONES[tone]) {
    const from = start + note.at
    const to = from + note.length
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    oscillator.frequency.value = note.hz
    gain.gain.setValueAtTime(0, from)
    gain.gain.linearRampToValueAtTime(VOLUME, from + RAMP)
    gain.gain.setValueAtTime(VOLUME, to - RAMP)
    gain.gain.linearRampToValueAtTime(0, to)
    oscillator.connect(gain).connect(context.destination)
    oscillator.onended = () => {
      oscillator.disconnect()
      gain.disconnect()
    }
    oscillator.start(from)
    oscillator.stop(to)
    end = Math.max(end, note.at + note.length)
  }
  return end
}

/** The Chinese ringback cadence: 450 Hz, 1 s on, 4 s off. */
const RING_HZ = 450
const RING_ON = 1
const RING_PERIOD = 5
const RING_RAMP = 0.01
/** Longer than any dial (the connect timeout is 25 s). */
const RING_MAX = 60

/** Ring until the returned stop is called: the server answers once the greeting is made. */
export function startRingback(context: BaseAudioContext): () => void {
  const start = context.currentTime + 0.01
  const oscillator = context.createOscillator()
  const gain = context.createGain()
  oscillator.frequency.value = RING_HZ
  gain.gain.setValueAtTime(0, start)
  for (let at = 0; at < RING_MAX; at += RING_PERIOD) {
    const from = start + at
    const to = from + RING_ON
    gain.gain.setValueAtTime(0, from)
    gain.gain.linearRampToValueAtTime(VOLUME, from + RING_RAMP)
    gain.gain.setValueAtTime(VOLUME, to - RING_RAMP)
    gain.gain.linearRampToValueAtTime(0, to)
  }
  oscillator.connect(gain).connect(context.destination)
  oscillator.onended = () => {
    oscillator.disconnect()
    gain.disconnect()
  }
  oscillator.start(start)
  oscillator.stop(start + RING_MAX)
  let stopped = false
  return () => {
    if (stopped) return
    stopped = true
    const now = context.currentTime
    gain.gain.cancelScheduledValues(now)
    gain.gain.setValueAtTime(gain.gain.value, now)
    gain.gain.linearRampToValueAtTime(0, now + RING_RAMP)
    try {
      oscillator.stop(now + RING_RAMP)
    } catch {
      // Already stopped.
    }
  }
}

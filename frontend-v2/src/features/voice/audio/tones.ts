// Call progress tones (spec §8), synthesised so there is nothing to ship. They
// go straight to the destination, never through the playback queue, so a
// `playback.clear` cannot cut them.

export type Tone = "connecting" | "connected" | "ended" | "error"

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
  // Two short beeps, 80 ms each with a 120 ms gap.
  connecting: [
    { hz: 440, at: 0, length: 0.08 },
    { hz: 440, at: 0.2, length: 0.08 },
  ],
  connected: [
    { hz: 660, at: 0, length: 0.09 },
    { hz: 880, at: 0.09, length: 0.09 },
  ],
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

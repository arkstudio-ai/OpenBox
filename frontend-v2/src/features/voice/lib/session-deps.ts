// Everything a call touches in the browser, behind one seam so the session's
// logic can be tested with stand-ins.
import { ensureAssistant, fetchVoiceTicket, voiceSocketUrl } from "../api/voice"
import {
  callAudioSupported,
  requestMicrophone,
  startCapture,
  type Capture,
  type PacketHandler,
} from "../audio/capture"
import { PcmPlayer } from "../audio/player"
import { playTone, startRingback, type Tone } from "../audio/tones"

export interface Player {
  readonly busy: boolean
  enqueue: (raw: ArrayBuffer) => number | null
  clear: () => number
  remaining: () => number
  level: () => number
  dispose: () => void
}

export interface SessionDeps {
  supported: () => boolean
  createContext: () => AudioContext
  requestMicrophone: () => Promise<MediaStream>
  startCapture: (context: AudioContext, stream: MediaStream, onPacket: PacketHandler) => Promise<Capture>
  createPlayer: (context: AudioContext, rate: number, onIdle: () => void) => Player
  playTone: (context: AudioContext, tone: Tone) => number
  /** Rings until the returned stop is called. */
  ringback: (context: AudioContext) => () => void
  fetchTicket: () => Promise<string>
  ensureAssistant: () => Promise<void>
  socketUrl: (ticket: string) => string
}

export const browserDeps: SessionDeps = {
  supported: callAudioSupported,
  createContext: () => new AudioContext({ latencyHint: "interactive" }),
  requestMicrophone,
  startCapture,
  createPlayer: (context, rate, onIdle) => new PcmPlayer(context, rate, onIdle),
  playTone,
  ringback: startRingback,
  fetchTicket: fetchVoiceTicket,
  ensureAssistant,
  socketUrl: voiceSocketUrl,
}

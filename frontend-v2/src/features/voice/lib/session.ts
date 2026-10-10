// One voice call, from the click to the ended panel (spec §3–§5): microphone,
// socket, speaker and tones. Not React — a call outlives any component — so it
// reports through a sink the store provides, and the reducer turns that into
// what the window shows.
import { emitAppEvent } from "@/shared/events/bus"
import { micFailure, type Capture } from "../audio/capture"
import { contextToPerformanceTime } from "../audio/player"
import type { Tone } from "../audio/tones"
import { closeEnding, isNormalEnding, type Ending } from "./endings"
import type { CallAction } from "./reducer"
import { browserDeps, type Player, type SessionDeps } from "./session-deps"
import type { EndReason, ServerEvent } from "./types"

/** A call the server has not readied by then is given up (spec §5.6). */
const CONNECT_TIMEOUT_MS = 25_000
/** The server sends a heartbeat every 10 s; this long without any frame is a dead line. */
const SILENCE_TIMEOUT_MS = 30_000
/** After `stop`, how long `ended` may take before the socket is closed anyway. */
const STOP_GRACE_MS = 4_000
const LEVEL_INTERVAL_MS = 50
/** Unsent audio beyond this means the network cannot keep up (as in the demo). */
const BACKLOG_BYTES = 128_000
/** The goodbye after a limit may keep playing this long once the call is over. */
const DRAIN_LIMIT_MS = 8_000
const OUTPUT_RATE = 24_000
/** Output RMS is small for speech; this spreads it over the orb's 0–1 range. */
const OUT_LEVEL_GAIN = 5

export interface SessionSink {
  dispatch: (action: CallAction) => void
  /** A latency mark; `t` defaults to now on the performance clock. */
  mark: (name: string, t?: number) => void
}

const round = (value: number) => Math.round(Math.min(1, value) * 100) / 100

export class VoiceCallSession {
  private readonly sink: SessionSink
  private readonly deps: SessionDeps
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private capture: Capture | null = null
  private player: Player | null = null
  private socket: WebSocket | null = null
  private ready = false
  /** The user hung up and `ended` is on its way. */
  private stopping = false
  /** Over: nothing more reaches the sink. */
  private done = false
  /** The server announced the time limit and is saying goodbye. */
  private limited = false
  private retried = false
  private muted = false
  private heardAudio = false
  private micLevel = 0
  private connectTimer: number | undefined
  private silenceTimer: number | undefined
  private stopTimer: number | undefined
  private levelTimer: number | undefined
  /** Stops the ringback; set while the call is being answered. */
  private stopRinging: (() => void) | null = null

  constructor(sink: SessionSink, deps: Partial<SessionDeps> = {}) {
    this.sink = sink
    this.deps = { ...browserDeps, ...deps }
  }

  /** Call from the click itself: an AudioContext may only start inside a user gesture. */
  start(): void {
    this.sink.mark("click")
    this.sink.dispatch({ type: "start" })
    let context: AudioContext
    try {
      if (!this.deps.supported()) throw new Error("unsupported")
      context = this.deps.createContext()
    } catch {
      this.finish({ reason: "unsupported" })
      return
    }
    this.context = context
    void context.resume().catch(() => undefined)
    window.addEventListener("pagehide", this.handlePageHide)
    void this.connect(context)
  }

  hangUp(): void {
    if (this.done || this.stopping) return
    // Before `ready` there is no call to settle: cancelling is just leaving.
    if (!this.ready) {
      this.finish({ reason: "hangup" })
      return
    }
    this.stopping = true
    this.sink.mark("hang_up")
    this.sink.dispatch({ type: "hang_up", at: Date.now() })
    this.send({ type: "stop" })
    // Hanging up is immediate for the user — no more listening or talking —
    // while the server takes a moment to settle the cost.
    this.releaseMicrophone()
    this.player?.clear()
    window.clearInterval(this.levelTimer)
    this.tone("ended")
    this.stopTimer = window.setTimeout(() => this.finish({ reason: "hangup" }), STOP_GRACE_MS)
  }

  setMuted(muted: boolean): void {
    this.muted = muted
    this.capture?.setMuted(muted)
  }

  /** Leave at once (the page is going away, the account changed): a best-effort `stop`, no waiting. */
  abandon(): void {
    if (this.done) return
    if (this.ready && !this.stopping) this.send({ type: "stop" })
    this.finish({ reason: "hangup" })
  }

  private async connect(context: AudioContext): Promise<void> {
    let stream: MediaStream
    try {
      stream = await this.deps.requestMicrophone()
    } catch (error) {
      this.finish({ reason: micFailure(error) })
      return
    }
    if (this.done) {
      for (const track of stream.getTracks()) track.stop()
      return
    }
    this.stream = stream
    stream.getAudioTracks()[0]?.addEventListener("ended", this.handleTrackEnded)
    this.sink.mark("mic_granted")
    this.sink.dispatch({ type: "mic_granted" })
    // Only now, not before the permission prompt: a ring under the browser's dialog is noise.
    // It rings until the server answers, which it does once the greeting is made, so that plays
    // in one piece, like a person picking up.
    this.ring()
    this.connectTimer = window.setTimeout(
      () => this.finish({ reason: "error", errorKey: "connectFailed" }),
      CONNECT_TIMEOUT_MS,
    )
    // The worklet loads while the ticket is fetched and the server dials the
    // model; audio waits for `ready` either way.
    this.deps.startCapture(context, stream, this.handlePacket).then(
      (capture) => {
        if (this.done) capture.stop()
        else {
          this.capture = capture
          capture.setMuted(this.muted)
        }
      },
      () => this.finish({ reason: "error", errorKey: "connectFailed" }),
    )
    try {
      const ticket = await this.deps.fetchTicket()
      if (this.done) return
      this.sink.mark("ticket")
      this.open(ticket)
    } catch {
      this.finish({ reason: "error", errorKey: "connectFailed" })
    }
  }

  private open(ticket: string): void {
    const socket = new WebSocket(this.deps.socketUrl(ticket))
    socket.binaryType = "arraybuffer"
    this.socket = socket
    socket.onopen = () => {
      if (this.socket === socket) this.sink.mark("socket_open")
    }
    socket.onmessage = (event: MessageEvent<unknown>) => {
      if (this.socket === socket) this.receive(event.data)
    }
    // An error is always followed by a close, and the close carries the code.
    socket.onclose = (event: CloseEvent) => {
      if (this.socket === socket) this.handleClose(event.code)
    }
  }

  private receive(data: unknown): void {
    if (this.done) return
    if (this.ready) this.watchSilence()
    if (data instanceof ArrayBuffer) {
      this.play(data)
      return
    }
    if (typeof data !== "string") return
    let event: ServerEvent
    try {
      event = JSON.parse(data) as ServerEvent
    } catch {
      return
    }
    if (event && typeof event.type === "string") this.handle(event)
  }

  private handle(event: ServerEvent): void {
    switch (event.type) {
      case "ready":
        if (this.ready) return
        this.onReady(event.output_sample_rate)
        break
      case "phase":
        this.sink.mark(`phase:${event.value}`)
        break
      case "playback.clear":
        this.sink.mark("playback_clear_received")
        if (this.player && this.player.clear() > 0) this.sink.mark("playback_stopped")
        this.sink.dispatch({ type: "playing", playing: false })
        break
      case "phrase":
        this.sink.mark(`phrase:${event.key}`)
        break
      case "turn":
        this.sink.mark(`turn:${event.state}`)
        // The same turn shows up in the text conversation; bring it into view
        // there. Its message exists once the assistant picks the turn up, so the
        // id arrives with `working`, not with `accepted`.
        if (event.message_id && (event.state === "accepted" || event.state === "working"))
          emitAppEvent("chat.reveal", { messageId: event.message_id })
        break
      case "limit":
        this.limited = true
        break
      case "error":
      case "ended":
        this.sink.dispatch({ type: "server", event, at: Date.now() })
        this.teardown(event.type === "error" ? "error" : event.reason)
        return
    }
    this.sink.dispatch({ type: "server", event, at: Date.now() })
  }

  private onReady(rate: number | undefined): void {
    if (!this.context) return
    this.ready = true
    window.clearTimeout(this.connectTimer)
    this.sink.mark("ready")
    this.player = this.deps.createPlayer(this.context, rate || OUTPUT_RATE, this.handleIdle)
    this.quietRing()
    this.watchSilence()
    this.levelTimer = window.setInterval(this.sampleLevels, LEVEL_INTERVAL_MS)
  }

  private play(raw: ArrayBuffer): void {
    // Audio before `ready` or after a hang-up belongs to nobody.
    if (!this.ready || this.stopping || !this.player || !this.context) return
    const received = performance.now()
    const burst = !this.player.busy
    const start = this.player.enqueue(raw)
    if (start === null) return
    const audible = contextToPerformanceTime(this.context, start)
    if (!this.heardAudio) {
      this.heardAudio = true
      this.sink.mark("first_audio_received", received)
      this.sink.mark("first_audio_started", audible)
    }
    if (burst) {
      // Every stretch of speech after silence, so each reply's latency can be read.
      this.sink.mark("audio_received", received)
      this.sink.mark("audio_started", audible)
      this.sink.dispatch({ type: "playing", playing: true })
    }
  }

  private handleClose(code: number): void {
    this.socket = null
    if (this.done) return
    if (code === 4404 && !this.ready && !this.retried) {
      this.retried = true
      void this.retryAfterEnsure()
      return
    }
    // The server let go before `ended` reached us: the hang-up still stands.
    if (this.stopping) this.finish({ reason: "hangup" })
    else this.finish(this.limited ? { reason: "limit" } : closeEnding(code, this.ready))
  }

  /** The assistant's main conversation did not exist yet: create it and dial once more. */
  private async retryAfterEnsure(): Promise<void> {
    try {
      await this.deps.ensureAssistant()
      const ticket = await this.deps.fetchTicket()
      if (this.done) return
      this.sink.mark("ticket")
      this.open(ticket)
    } catch {
      this.finish({ reason: "error", errorKey: "assistantUnavailable" })
    }
  }

  private readonly handlePacket = (audio: ArrayBuffer, level: number): void => {
    this.micLevel = level
    const socket = this.socket
    // No audio before `ready` (spec §5.1), and none once the user has hung up.
    if (!this.ready || this.stopping || this.done || socket?.readyState !== WebSocket.OPEN) return
    if (socket.bufferedAmount > BACKLOG_BYTES) {
      this.finish({ reason: "error", errorKey: "backlog" })
      return
    }
    socket.send(audio)
  }

  private readonly sampleLevels = (): void => {
    const mic = this.muted ? 0 : round(this.micLevel)
    const out = round((this.player?.level() ?? 0) * OUT_LEVEL_GAIN)
    this.sink.dispatch({ type: "level", mic, out })
  }

  private readonly handleIdle = (): void => {
    if (!this.done) this.sink.dispatch({ type: "playing", playing: false })
  }

  private readonly handlePageHide = (): void => this.abandon()

  private readonly handleTrackEnded = (): void => this.finish({ reason: "mic_lost" })

  private watchSilence(): void {
    window.clearTimeout(this.silenceTimer)
    this.silenceTimer = window.setTimeout(() => this.finish({ reason: "network" }), SILENCE_TIMEOUT_MS)
  }

  private send(payload: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(payload))
  }

  private ring(): void {
    try {
      this.stopRinging = this.context ? this.deps.ringback(this.context) : null
    } catch {
      this.stopRinging = null
    }
  }

  private quietRing(): void {
    this.stopRinging?.()
    this.stopRinging = null
  }

  private tone(tone: Tone): number {
    try {
      return this.context ? this.deps.playTone(this.context, tone) : 0
    } catch {
      return 0
    }
  }

  /** An ending the client decided. */
  private finish(ending: Ending): void {
    if (this.done) return
    this.sink.dispatch({ type: "end", reason: ending.reason, errorKey: ending.errorKey, at: Date.now() })
    this.teardown(ending.reason)
  }

  private teardown(reason: EndReason): void {
    this.done = true
    this.quietRing()
    window.clearTimeout(this.connectTimer)
    window.clearTimeout(this.silenceTimer)
    window.clearTimeout(this.stopTimer)
    window.clearInterval(this.levelTimer)
    window.removeEventListener("pagehide", this.handlePageHide)
    this.releaseMicrophone()
    const socket = this.socket
    this.socket = null
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close(1000)
    this.sink.mark("ended")
    this.releaseSpeaker(reason)
  }

  private releaseMicrophone(): void {
    this.capture?.stop()
    this.capture = null
    const stream = this.stream
    this.stream = null
    if (!stream) return
    stream.getAudioTracks()[0]?.removeEventListener("ended", this.handleTrackEnded)
    for (const track of stream.getTracks()) track.stop()
  }

  /** The goodbye after a limit is the call's last words, so it plays out;
   *  anything else stops at once. Then the closing tone, unless the hang-up
   *  already played it, and the context goes. */
  private releaseSpeaker(reason: EndReason): void {
    const { context, player } = this
    const limit = reason === "limit" || this.limited
    const drain = limit && player ? Math.min(player.remaining() * 1000, DRAIN_LIMIT_MS) : 0
    if (!drain) player?.clear()
    window.setTimeout(() => {
      player?.dispose()
      if (!context) return
      const tail = this.stopping ? 0 : this.tone(limit || isNormalEnding(reason) ? "ended" : "error")
      window.setTimeout(() => void context.close().catch(() => undefined), tail * 1000 + 200)
    }, drain)
  }
}

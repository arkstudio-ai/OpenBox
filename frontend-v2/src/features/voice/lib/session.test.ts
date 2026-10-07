import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { onAppEvent } from "@/shared/events/bus"
import type { PacketHandler } from "../audio/capture"
import { callReducer, initialCall } from "./reducer"
import { VoiceCallSession } from "./session"
import type { SessionDeps } from "./session-deps"
import type { CallState, ServerEvent } from "./types"

class FakeSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readonly url: string
  readyState = FakeSocket.CONNECTING
  binaryType = "blob"
  bufferedAmount = 0
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  readonly sent: unknown[] = []

  constructor(url: string) {
    this.url = url
    sockets.push(this)
  }

  open(): void {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
  }

  receive(frame: ServerEvent | ArrayBuffer): void {
    this.onmessage?.({ data: frame instanceof ArrayBuffer ? frame : JSON.stringify(frame) })
  }

  /** The server closing with a code. */
  drop(code: number): void {
    this.readyState = FakeSocket.CLOSED
    this.onclose?.({ code })
  }

  send(data: unknown): void {
    this.sent.push(data)
  }

  close(code = 1005): void {
    if (this.readyState === FakeSocket.CLOSED) return
    this.readyState = FakeSocket.CLOSED
    this.onclose?.({ code })
  }

  get messages(): unknown[] {
    return this.sent.filter((data) => typeof data === "string").map((data) => JSON.parse(data as string))
  }

  get audio(): unknown[] {
    return this.sent.filter((data) => data instanceof ArrayBuffer)
  }
}

const READY: ServerEvent = {
  type: "ready",
  call_id: "call-1",
  model: "qwen3.8-omni-flash-realtime",
  input_sample_rate: 16000,
  output_sample_rate: 24000,
  max_seconds: 1800,
  price_date: "2026-10-07",
}

let sockets: FakeSocket[]
let sessions: VoiceCallSession[]

function harness(overrides: Partial<SessionDeps> = {}) {
  let state: CallState = initialCall
  const marks: string[] = []
  const tones: string[] = []
  const rings: string[] = []
  const track = { enabled: true, stop: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
  const capture = { setMuted: vi.fn(), stop: vi.fn() }
  const player = {
    busy: false,
    enqueue: vi.fn((): number | null => 10.5),
    clear: vi.fn(() => 1),
    remaining: vi.fn(() => 0),
    level: vi.fn(() => 0),
    dispose: vi.fn(),
  }
  const context = {
    currentTime: 10,
    resume: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
  }
  let onPacket: PacketHandler = () => undefined
  const deps: SessionDeps = {
    supported: () => true,
    createContext: () => context as unknown as AudioContext,
    requestMicrophone: vi.fn(async () => stream),
    startCapture: vi.fn(async (_context: AudioContext, _stream: MediaStream, handler: PacketHandler) => {
      onPacket = handler
      return capture
    }),
    createPlayer: vi.fn(() => player),
    playTone: (_context: AudioContext, tone: string) => {
      tones.push(tone)
      return 0.2
    },
    ringback: () => {
      rings.push("ring")
      return () => rings.push("answered")
    },
    fetchTicket: vi.fn(async () => `ticket-${sockets.length + 1}`),
    ensureAssistant: vi.fn(async () => undefined),
    socketUrl: (ticket: string) => `ws://app.test/ws/assistant/voice?ticket=${ticket}`,
    ...overrides,
  }
  const session = new VoiceCallSession(
    {
      dispatch: (action) => {
        state = callReducer(state, action)
      },
      mark: (name) => {
        marks.push(name)
      },
    },
    deps,
  )
  sessions.push(session)
  return {
    session,
    deps,
    track,
    capture,
    player,
    context,
    marks,
    tones,
    rings,
    get state() {
      return state
    },
    packet: (level = 0.3) => onPacket(new Int16Array(1600).buffer, level),
  }
}

const settle = () => vi.advanceTimersByTimeAsync(0)

async function connectedCall(overrides: Partial<SessionDeps> = {}) {
  const call = harness(overrides)
  call.session.start()
  await settle()
  sockets[0].open()
  sockets[0].receive(READY)
  return call
}

beforeEach(() => {
  sockets = []
  sessions = []
  vi.useFakeTimers()
  vi.stubGlobal("WebSocket", FakeSocket)
})

afterEach(() => {
  for (const session of sessions) session.abandon()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("dialling", () => {
  it("asks for the microphone, then a voice ticket, then dials — and sends no audio before `ready`", async () => {
    const call = harness()
    call.session.start()
    expect(call.state.status).toBe("requesting_mic")
    expect(call.context.resume).toHaveBeenCalled()
    await settle()
    expect(call.state.status).toBe("connecting")
    // It rings until the server answers (once the greeting is made).
    expect(call.rings).toEqual(["ring"])
    expect(call.tones).toEqual([])
    const order = (fn: unknown) => vi.mocked(fn as () => void).mock.invocationCallOrder[0]
    expect(order(call.deps.requestMicrophone)).toBeLessThan(order(call.deps.fetchTicket))
    expect(sockets.map((socket) => socket.url)).toEqual(["ws://app.test/ws/assistant/voice?ticket=ticket-1"])
    expect(sockets[0].binaryType).toBe("arraybuffer")

    sockets[0].open()
    call.packet()
    expect(sockets[0].audio).toHaveLength(0)
    sockets[0].receive(READY)
    expect(call.state).toMatchObject({ status: "connected", phase: "greeting", callId: "call-1" })
    // Answered: the ringing stops and the greeting plays; no chime over it.
    expect(call.rings).toEqual(["ring", "answered"])
    expect(call.tones).toEqual([])
    call.packet()
    expect(sockets[0].audio).toHaveLength(1)
    expect(call.marks).toEqual(["click", "mic_granted", "ticket", "socket_open", "ready"])
  })

  it("ends as unsupported without asking for anything when the browser cannot do call audio", () => {
    const call = harness({ supported: () => false })
    call.session.start()
    expect(call.state.ended?.reason).toBe("unsupported")
    expect(call.deps.requestMicrophone).not.toHaveBeenCalled()
  })

  it.each([
    ["NotAllowedError", "mic_denied"],
    ["NotFoundError", "mic_missing"],
    ["NotReadableError", "mic_busy"],
  ])("ends a refused microphone (%s) as %s, before any ticket", async (name, reason) => {
    const call = harness({ requestMicrophone: () => Promise.reject(new DOMException("refused", name)) })
    call.session.start()
    await settle()
    expect(call.state.ended?.reason).toBe(reason)
    expect(call.deps.fetchTicket).not.toHaveBeenCalled()
    expect(sockets).toHaveLength(0)
  })

  it("cannot get a ticket: connect failed", async () => {
    const call = harness({ fetchTicket: () => Promise.reject(new Error("ticket refused: 503")) })
    call.session.start()
    await settle()
    expect(call.state.ended).toMatchObject({ reason: "error", errorKey: "connectFailed" })
    expect(call.track.stop).toHaveBeenCalled()
  })

  it("gives up after 25 s without `ready`", async () => {
    const call = harness()
    call.session.start()
    await settle()
    sockets[0].open()
    await vi.advanceTimersByTimeAsync(24_999)
    expect(call.state.status).toBe("connecting")
    await vi.advanceTimersByTimeAsync(1)
    expect(call.state.ended).toMatchObject({ reason: "error", errorKey: "connectFailed" })
    expect(sockets[0].readyState).toBe(FakeSocket.CLOSED)
    expect(call.rings).toEqual(["ring", "answered"]) // the ringing never outlives the dial
  })

  it("creates the assistant's conversation and dials once more on 4404", async () => {
    const call = harness()
    call.session.start()
    await settle()
    sockets[0].drop(4404)
    await settle()
    expect(call.deps.ensureAssistant).toHaveBeenCalledTimes(1)
    expect(sockets.map((socket) => socket.url.split("ticket=")[1])).toEqual(["ticket-1", "ticket-2"])
    sockets[1].drop(4404)
    expect(call.state.ended).toMatchObject({ reason: "error", errorKey: "assistantUnavailable" })
  })

  it("cancelling before `ready` just leaves, with no `stop` to send", async () => {
    const call = harness()
    call.session.start()
    await settle()
    sockets[0].open()
    call.session.hangUp()
    expect(call.state.ended?.reason).toBe("hangup")
    expect(sockets[0].messages).toEqual([])
    expect(sockets[0].readyState).toBe(FakeSocket.CLOSED)
  })
})

describe("close codes", () => {
  it.each([
    [4009, false, { reason: "concurrent" }],
    [4029, false, { reason: "quota" }],
    [4503, false, { reason: "error", errorKey: "disabled" }],
    [4001, false, { reason: "error", errorKey: "connectFailed" }],
    [1006, false, { reason: "error", errorKey: "connectFailed" }],
    [1011, true, { reason: "error", errorKey: "connectFailed" }],
    [1006, true, { reason: "network", errorKey: null }],
  ])("%i (after ready: %s) ends as %o", async (code, ready, ending) => {
    const call = harness()
    call.session.start()
    await settle()
    sockets[0].open()
    if (ready) sockets[0].receive(READY)
    sockets[0].drop(code)
    expect(call.state.ended).toMatchObject(ending)
  })
})

describe("in the call", () => {
  it("plays model audio, cuts it on `playback.clear`, and marks the timings", async () => {
    const call = await connectedCall()
    sockets[0].receive(new ArrayBuffer(4800))
    expect(call.player.enqueue).toHaveBeenCalledTimes(1)
    expect(call.state.playing).toBe(true)
    sockets[0].receive({ type: "phase", value: "listening" })
    sockets[0].receive({ type: "playback.clear" })
    expect(call.player.clear).toHaveBeenCalled()
    expect(call.state.playing).toBe(false)
    expect(call.marks).toEqual([
      "click",
      "mic_granted",
      "ticket",
      "socket_open",
      "ready",
      "first_audio_received",
      "first_audio_started",
      "audio_received",
      "audio_started",
      "phase:listening",
      "playback_clear_received",
      "playback_stopped",
    ])
  })

  it("shows the turn's message once the assistant picks it up (the id comes with working)", async () => {
    const shown: string[] = []
    const stop = onAppEvent("chat.reveal", ({ messageId }) => shown.push(messageId))
    await connectedCall()
    sockets[0].receive({ type: "turn", turn_id: "t2", state: "accepted", inbox_id: "i2", message_id: null })
    sockets[0].receive({ type: "turn", turn_id: "t2", state: "working", inbox_id: "i2", message_id: "m2" })
    stop()
    expect(shown).toEqual(["m2"])
  })

  it("asks the text conversation to show an accepted turn, once", async () => {
    const shown: string[] = []
    const stop = onAppEvent("chat.reveal", ({ messageId }) => shown.push(messageId))
    const call = await connectedCall()
    sockets[0].receive({ type: "turn", turn_id: "t1", state: "accepted", inbox_id: "i1", message_id: "m1" })
    sockets[0].receive({ type: "phrase", key: "progress" })
    sockets[0].receive({ type: "turn", turn_id: "t1", state: "delivered", inbox_id: "i1", message_id: "m1" })
    stop()
    expect(shown).toEqual(["m1"])
    expect(call.state.turns).toEqual({ t1: { state: "delivered", messageId: "m1" } })
    expect(call.marks.slice(5)).toEqual(["turn:accepted", "phrase:progress", "turn:delivered"])
  })

  it("mutes the capture, which keeps sending (silent) frames", async () => {
    const call = await connectedCall()
    call.session.setMuted(true)
    expect(call.capture.setMuted).toHaveBeenLastCalledWith(true)
    call.packet(0)
    expect(sockets[0].audio).toHaveLength(1)
  })

  it("ends the call when unsent audio backs up", async () => {
    const call = await connectedCall()
    sockets[0].bufferedAmount = 128_001
    call.packet()
    expect(call.state.ended).toMatchObject({ reason: "error", errorKey: "backlog" })
    expect(sockets[0].audio).toHaveLength(0)
  })

  it("ends as a network failure after 30 s without any frame", async () => {
    const call = await connectedCall()
    await vi.advanceTimersByTimeAsync(20_000)
    sockets[0].receive({ type: "heartbeat", elapsed_seconds: 20 })
    await vi.advanceTimersByTimeAsync(29_999)
    expect(call.state.status).toBe("connected")
    await vi.advanceTimersByTimeAsync(1)
    expect(call.state.ended?.reason).toBe("network")
  })

  it("ends as mic_lost when the microphone goes away", async () => {
    const call = await connectedCall()
    const [, onEnded] = call.track.addEventListener.mock.calls[0] as [string, () => void]
    onEnded()
    expect(call.state.ended?.reason).toBe("mic_lost")
  })

  it("shows the server's error message and closes", async () => {
    const call = await connectedCall()
    sockets[0].receive({ type: "error", code: "provider_error", message: "语音服务出了点问题" })
    expect(call.state.ended).toMatchObject({ reason: "error", errorMessage: "语音服务出了点问题" })
    expect(sockets[0].readyState).toBe(FakeSocket.CLOSED)
    await vi.advanceTimersByTimeAsync(0)
    expect(call.tones.at(-1)).toBe("error")
  })

  it("lets the goodbye after a limit play out before the speaker goes", async () => {
    const call = await connectedCall()
    call.player.remaining.mockReturnValue(2)
    sockets[0].receive({ type: "limit", reason: "max_duration", elapsed_seconds: 1800 })
    sockets[0].receive({
      type: "ended",
      reason: "limit",
      duration_seconds: 1800,
      pending_turns: 0,
      cost: null,
    })
    expect(call.state.ended?.reason).toBe("limit")
    expect(call.player.clear).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1_999)
    expect(call.player.dispose).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(call.player.dispose).toHaveBeenCalled()
    expect(call.tones.at(-1)).toBe("ended")
    await vi.advanceTimersByTimeAsync(500)
    expect(call.context.close).toHaveBeenCalled()
  })
})

describe("hanging up", () => {
  it("is immediate for the user: `stop` sent, microphone and speaker released, clock stopped", async () => {
    const call = await connectedCall()
    call.session.hangUp()
    expect(sockets[0].messages).toEqual([{ type: "stop" }])
    expect(call.marks.at(-1)).toBe("hang_up")
    expect(call.capture.stop).toHaveBeenCalled()
    expect(call.track.stop).toHaveBeenCalled()
    expect(call.player.clear).toHaveBeenCalled()
    expect(call.state.status).toBe("ending")
    expect(call.tones.at(-1)).toBe("ended")
    call.packet()
    sockets[0].receive(new ArrayBuffer(4800))
    expect(sockets[0].audio).toHaveLength(0)
    expect(call.player.enqueue).not.toHaveBeenCalled()
  })

  it("shows the server's `ended` with its cost and pending turns as soon as it comes", async () => {
    const call = await connectedCall()
    call.session.hangUp()
    sockets[0].receive({
      type: "ended",
      reason: "hangup",
      duration_seconds: 42,
      pending_turns: 1,
      cost: { total_yuan: "0.0042", final: true },
    })
    expect(call.state.ended).toMatchObject({
      reason: "hangup",
      durationSeconds: 42,
      pendingTurns: 1,
      cost: { total_yuan: "0.0042" },
    })
    expect(sockets[0].readyState).toBe(FakeSocket.CLOSED)
    // The hang-up already said goodbye.
    await vi.advanceTimersByTimeAsync(1_000)
    expect(call.tones.filter((tone) => tone === "ended")).toHaveLength(1)
  })

  it("closes on its own 4 s after `stop` when no `ended` comes", async () => {
    const call = await connectedCall()
    call.session.hangUp()
    await vi.advanceTimersByTimeAsync(3_999)
    expect(sockets[0].readyState).toBe(FakeSocket.OPEN)
    expect(call.state.status).toBe("ending")
    await vi.advanceTimersByTimeAsync(1)
    expect(sockets[0].readyState).toBe(FakeSocket.CLOSED)
    expect(call.state.ended?.reason).toBe("hangup")
  })

  it("sends `stop` when the page goes away", async () => {
    const call = await connectedCall()
    window.dispatchEvent(new Event("pagehide"))
    expect(sockets[0].messages).toEqual([{ type: "stop" }])
    expect(call.state.ended?.reason).toBe("hangup")
    expect(sockets[0].readyState).toBe(FakeSocket.CLOSED)
    expect(call.track.stop).toHaveBeenCalled()
  })
})

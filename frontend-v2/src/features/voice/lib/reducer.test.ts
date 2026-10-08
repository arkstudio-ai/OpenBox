import { describe, expect, it } from "vitest"
import { callReducer, displayPhase, initialCall, type CallAction } from "./reducer"
import type { CallState, ServerEvent } from "./types"

const READY: ServerEvent = {
  type: "ready",
  call_id: "call-1",
  model: "qwen3.8-omni-flash-realtime",
  input_sample_rate: 16000,
  output_sample_rate: 24000,
  max_seconds: 1800,
  price_date: "2026-10-07",
}

const server = (event: ServerEvent, at = 1_000): CallAction => ({ type: "server", event, at })

function run(...actions: CallAction[]): CallState {
  return actions.reduce(callReducer, initialCall)
}

const connected = () => run({ type: "start" }, { type: "mic_granted" }, server(READY, 1_000))

describe("callReducer", () => {
  it("walks the dial-up: start → requesting_mic → connecting → ready → connected/greeting", () => {
    expect(run({ type: "start" }).status).toBe("requesting_mic")
    expect(run({ type: "start" }, { type: "mic_granted" }).status).toBe("connecting")
    const call = connected()
    expect(call).toMatchObject({
      status: "connected",
      phase: "greeting",
      callId: "call-1",
      maxSeconds: 1800,
      startedAt: 1_000,
    })
  })

  it("ignores `ready` outside connecting and everything once idle", () => {
    expect(run({ type: "start" }, server(READY)).status).toBe("requesting_mic")
    expect(run(server(READY)).status).toBe("idle")
    expect(run(server({ type: "phase", value: "listening" })).status).toBe("idle")
  })

  it("takes the phase and its working/late flags from the server only", () => {
    let call = callReducer(connected(), server({ type: "phase", value: "listening" }))
    expect(call).toMatchObject({ phase: "listening", working: false, late: false })
    call = callReducer(call, server({ type: "phase", value: "speaking", working: true }))
    expect(call).toMatchObject({ phase: "speaking", working: true, late: false })
    call = callReducer(call, server({ type: "phase", value: "working", working: true, late: true }))
    expect(call).toMatchObject({ phase: "working", working: true, late: true })
    for (const value of ["greeting", "thinking"] as const)
      expect(callReducer(call, server({ type: "phase", value })).phase).toBe(value)
  })

  it("keeps saying `speaking` while the reply still drains after the server moved to listening", () => {
    let call = callReducer(connected(), server({ type: "phase", value: "listening" }))
    call = callReducer(call, { type: "playing", playing: true })
    expect(displayPhase(call)).toBe("speaking")
    call = callReducer(call, { type: "playing", playing: false })
    expect(displayPhase(call)).toBe("listening")
  })

  it("accumulates turns and keeps a message id that later states omit", () => {
    let call = callReducer(
      connected(),
      server({ type: "turn", turn_id: "t1", state: "accepted", inbox_id: "i1", message_id: "m1" }),
    )
    call = callReducer(
      call,
      server({ type: "turn", turn_id: "t2", state: "accepted", inbox_id: "i2", message_id: "m2" }),
    )
    call = callReducer(
      call,
      server({ type: "turn", turn_id: "t1", state: "delivered", inbox_id: "i1", message_id: null }),
    )
    expect(call.turns).toEqual({
      t1: { state: "delivered", messageId: "m1" },
      t2: { state: "accepted", messageId: "m2" },
    })
  })

  it("keeps the latest cost snapshot", () => {
    const call = callReducer(connected(), server({ type: "cost", total_yuan: "0.003500", settled_rounds: 2 }))
    expect(call.cost).toMatchObject({ total_yuan: "0.003500", settled_rounds: 2 })
  })

  it("passes the server's ending through: reason, duration, pending turns and final cost", () => {
    const call = callReducer(
      connected(),
      server({
        type: "ended",
        reason: "hangup",
        duration_seconds: 74,
        pending_turns: 2,
        cost: { total_yuan: "0.0120", final: true },
      }),
    )
    expect(call.status).toBe("ended")
    expect(call.ended).toEqual({
      reason: "hangup",
      durationSeconds: 74,
      pendingTurns: 2,
      cost: { total_yuan: "0.0120", final: true },
      errorKey: null,
      errorMessage: null,
    })
  })

  it("ignores phases it has no words for and reads an unknown ending as an error", () => {
    const call = callReducer(connected(), server({ type: "phase", value: "listening" }))
    const unknown = { type: "phase", value: "dreaming" } as unknown as ServerEvent
    expect(callReducer(call, server(unknown))).toBe(call)
    const ended = { type: "ended", reason: "solar_flare", duration_seconds: 3, pending_turns: 0, cost: null }
    expect(callReducer(call, server(ended as unknown as ServerEvent)).ended?.reason).toBe("error")
  })

  it("ends at once on `error`, keeping the server's message for the panel", () => {
    let call = callReducer(connected(), server({ type: "cost", total_yuan: "0.0010" }))
    call = callReducer(
      call,
      server({ type: "error", code: "provider_error", message: "语音服务暂时不可用" }, 31_000),
    )
    expect(call.status).toBe("ended")
    expect(call.ended).toMatchObject({
      reason: "error",
      errorMessage: "语音服务暂时不可用",
      durationSeconds: 30,
      cost: { total_yuan: "0.0010" },
    })
  })

  it("ends as `limit` after the limit announcement, whether `ended` or a bare close comes next", () => {
    const announced = callReducer(
      connected(),
      server({ type: "limit", reason: "max_duration", elapsed_seconds: 1800 }),
    )
    expect(announced.status).toBe("connected")
    expect(
      callReducer(
        announced,
        server({ type: "ended", reason: "limit", duration_seconds: 1800, pending_turns: 0, cost: null }),
      ).ended?.reason,
    ).toBe("limit")
    expect(callReducer(announced, { type: "end", reason: "network", at: 2_000 }).ended?.reason).toBe("limit")
  })

  it("measures a client-side ending from the clock and counts unfinished turns", () => {
    let call = callReducer(
      connected(),
      server({ type: "turn", turn_id: "t1", state: "accepted", message_id: "m1" }),
    )
    call = callReducer(call, server({ type: "turn", turn_id: "t2", state: "late", message_id: "m2" }))
    call = callReducer(call, server({ type: "turn", turn_id: "t3", state: "delivered", message_id: "m3" }))
    call = callReducer(call, { type: "end", reason: "network", at: 13_400 })
    expect(call.ended).toMatchObject({
      reason: "network",
      durationSeconds: 12,
      pendingTurns: 2,
      errorKey: null,
    })
  })

  it("stops the clock at the hang-up while the server settles", () => {
    let call = callReducer(connected(), { type: "hang_up", at: 6_000 })
    expect(call).toMatchObject({ status: "ending", stoppedAt: 6_000 })
    call = callReducer(call, { type: "end", reason: "hangup", at: 10_000 })
    expect(call.ended).toMatchObject({ reason: "hangup", durationSeconds: 5 })
  })

  it("ends before connecting with no duration, keeping the copy key", () => {
    const call = run(
      { type: "start" },
      { type: "end", reason: "error", errorKey: "connectFailed", at: 5_000 },
    )
    expect(call.ended).toMatchObject({
      reason: "error",
      errorKey: "connectFailed",
      durationSeconds: 0,
      cost: null,
    })
    expect(run({ type: "start" }, { type: "end", reason: "mic_denied", at: 1 }).ended?.reason).toBe(
      "mic_denied",
    )
  })

  it("updates levels only while connected and only when they change", () => {
    const call = connected()
    const louder = callReducer(call, { type: "level", mic: 0.4, out: 0 })
    expect(louder.level).toEqual({ mic: 0.4, out: 0 })
    expect(callReducer(louder, { type: "level", mic: 0.4, out: 0 })).toBe(louder)
    expect(callReducer(run({ type: "start" }), { type: "level", mic: 1, out: 1 }).level).toEqual({
      mic: 0,
      out: 0,
    })
  })

  it("dismisses only an ended call, and a new start forgets the last one", () => {
    const ended = callReducer(connected(), { type: "end", reason: "network", at: 3_000 })
    expect(callReducer(ended, { type: "dismiss" })).toBe(initialCall)
    expect(callReducer(connected(), { type: "dismiss" }).status).toBe("connected")
    expect(callReducer(ended, { type: "start" })).toMatchObject({
      status: "requesting_mic",
      ended: null,
      cost: null,
      turns: {},
    })
  })
})

// Golden parity with backend/trajectory/projector.py. Expected values come from
// the backend fixture files, never from this implementation: every fixture the
// backend adds under trajectory/fixtures/ is picked up automatically.
import { describe, expect, it } from "vitest"
import type { AgentSummary, ProjectionState, TrajectoryEvent, TrajectoryStatistics } from "../types/protocol"
import { ingestCheckpoint } from "./adapter"
import { emptyState, eventsForRecord, reduce, reduceMany, replay } from "./projector"
import { agents, statistics } from "./statistics"
import { gtSeq, lteSeq } from "./seq"

interface GoldenFixture {
  name: string
  events: TrajectoryEvent[]
  expected_state: ProjectionState
  expected_statistics: TrajectoryStatistics
  expected_agents: AgentSummary[]
  historical?: { through_seq: string; state: ProjectionState }
}

const fixtures = Object.entries(
  import.meta.glob<GoldenFixture>("../../../../../backend/trajectory/fixtures/*.json", {
    eager: true,
    import: "default",
  }),
)

function deepFreeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value)
    for (const child of Object.values(value)) deepFreeze(child)
  }
  return value
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

it("finds the shared backend fixtures", () => {
  expect(fixtures.length).toBeGreaterThan(0)
})

describe.each(fixtures)("golden fixture %s", (_path, fixture) => {
  const events = fixture.events

  it("replays to the backend's expected state", () => {
    expect(replay(events)).toEqual(fixture.expected_state)
  })

  it("derives the backend's statistics and agent identities", () => {
    const state = replay(events)
    expect(statistics(state)).toEqual(fixture.expected_statistics)
    expect(agents(state)).toEqual(fixture.expected_agents)
  })

  it("matches the backend's historical state without leaking later values", () => {
    const historical = fixture.historical
    if (!historical) return
    const prefix = events.filter((event) => lteSeq(event.seq, historical.through_seq))
    const state = replay(prefix)
    expect(state).toEqual(historical.state)
    for (const record of Object.values(state.records)) {
      expect(gtSeq(record.as_of_seq, historical.through_seq)).toBe(false)
      if (record.end_seq) expect(gtSeq(record.end_seq, historical.through_seq)).toBe(false)
    }
  })

  it("reaches the same state from a checkpoint plus the tail", () => {
    const historical = fixture.historical
    if (!historical) return
    const response = {
      checkpoint: {
        through_seq: historical.through_seq,
        projector_version: 1,
        state: clone(historical.state),
      },
      through_seq: historical.through_seq,
    }
    const ingested = ingestCheckpoint(response, fixture.expected_state.through_seq)
    expect(ingested.kind).toBe("ok")
    if (ingested.kind !== "ok") return
    const tail = events.filter((event) => gtSeq(event.seq, historical.through_seq))
    expect(replay(tail, ingested.state)).toEqual(fixture.expected_state)
  })

  it("is independent of batch boundaries at every watermark", () => {
    let oneByOne = emptyState()
    events.forEach((event, index) => {
      oneByOne = reduce(oneByOne, event)
      expect(reduceMany(emptyState(), events.slice(0, index + 1))).toEqual(oneByOne)
    })
    for (const size of [2, 3, 7]) {
      let batched = emptyState()
      for (let start = 0; start < events.length; start += size)
        batched = reduceMany(batched, events.slice(start, start + size))
      expect(batched).toEqual(fixture.expected_state)
    }
  })

  it("ignores duplicate and stale deliveries", () => {
    const state = replay(events)
    expect(reduceMany(state, events)).toBe(state)
    const half = Math.floor(events.length / 2)
    const partial = replay(events.slice(0, half))
    const redelivered = reduceMany(partial, [...events.slice(0, half), ...events.slice(half)])
    expect(redelivered).toEqual(fixture.expected_state)
  })

  it("never mutates its inputs", () => {
    const frozenEvents = deepFreeze(clone(events))
    const base = deepFreeze(replay(clone(events).slice(0, 5)))
    expect(() => reduceMany(base, frozenEvents)).not.toThrow()
    expect(reduceMany(base, frozenEvents)).toEqual(fixture.expected_state)
  })

  it("selects the raw events behind a record like the server detail", () => {
    const state = replay(events)
    for (const record of Object.values(state.records)) {
      const own = eventsForRecord(record, events)
      for (const item of own) expect(gtSeq(item.seq, record.as_of_seq)).toBe(false)
      // System snapshots are derived from request.prepared, not targeted by it,
      // so the server attaches no events to them either.
      if (record.kind === "system") continue
      expect(own.length).toBeGreaterThan(0)
      expect(own[0].seq).toBe(record.start_seq)
    }
  })
})

/* ----------------------------- targeted cases ----------------------------- */

let counter = 0
function event(
  type: string,
  data: Record<string, unknown> = {},
  extra: Partial<TrajectoryEvent> = {},
): TrajectoryEvent {
  counter += 1
  return {
    event_id: `evt_${counter}`,
    user_id: "owner",
    session_id: "ses",
    seq: String(counter),
    type,
    version: 1,
    occurred_at: `2026-09-11T08:00:00.${String(counter).padStart(3, "0")}Z`,
    run_id: "run_1",
    data,
    ...extra,
  }
}

function fresh() {
  counter = 0
}

describe("unsupported content", () => {
  it("records an unknown version instead of dropping it", () => {
    fresh()
    const state = replay([
      event("tool.requested", { name: "bash" }, { call_id: "c" }),
      event("tool.finished", { status: "completed" }, { call_id: "c", version: 2 }),
    ])
    expect(state.through_seq).toBe("2")
    expect(state.unsupported_events).toEqual([{ seq: "2", type: "tool.finished", version: 2 }])
    expect(state.records["tool:c"].status).toBe("pending")
  })

  it("records an unknown type", () => {
    fresh()
    const state = replay([event("tool.teleported")])
    expect(state.unsupported_events).toEqual([{ seq: "1", type: "tool.teleported", version: 1 }])
    expect(state.records).toEqual({})
  })
})

describe("stream semantics", () => {
  it("appends tool deltas, replaces cumulative output and drops repeated chunks", () => {
    fresh()
    const call = { call_id: "c" }
    const state = replay([
      event("tool.requested", { name: "bash" }, call),
      event("tool.output", { mode: "delta", output: "a", chunk_index: 0 }, call),
      event("tool.output", { mode: "delta", output: "b", chunk_index: 1 }, call),
      event("tool.output", { mode: "delta", output: "b", chunk_index: 1 }, call),
    ])
    expect(state.records["tool:c"].data.output).toBe("ab")
    const replaced = reduce(
      state,
      event("tool.output", { mode: "replace", output: "full", chunk_index: 2 }, call),
    )
    expect(replaced.records["tool:c"].data.output).toBe("full")
  })

  it("sums delta usage and replaces cumulative usage", () => {
    fresh()
    const req = { request_id: "r" }
    const state = replay([
      event("request.started", {}, req),
      event("request.usage", { mode: "delta", usage: { output_tokens: 2 } }, req),
      event("request.usage", { mode: "delta", usage: { output_tokens: 3 } }, req),
    ])
    expect(state.records["request:r"].usage).toEqual({ output_tokens: 5 })
    const replaced = reduce(
      state,
      event("request.usage", { mode: "replace", usage: { output_tokens: 4, input_tokens: 1 } }, req),
    )
    expect(replaced.records["request:r"].usage).toEqual({ output_tokens: 4, input_tokens: 1 })
  })

  it("marks open work of an interrupted run as unknown without inventing results", () => {
    fresh()
    const state = replay([
      event("tool.requested", { name: "bash" }, { call_id: "open" }),
      event("tool.requested", { name: "bash" }, { call_id: "done" }),
      event("tool.finished", { status: "completed" }, { call_id: "done" }),
      event("run.interrupted", { reason: "worker lost" }),
    ])
    expect(state.records["tool:open"]).toMatchObject({
      status: "unknown",
      end_seq: "4",
      result_preview: null,
    })
    expect(state.records["tool:done"].status).toBe("completed")
    expect(state.records["interrupt:evt_4"].status).toBe("completed")
  })

  it("gives a tool that never started no execution duration, even if the finish reports one", () => {
    fresh()
    const call = { call_id: "c" }
    const state = replay([
      event("tool.requested", { name: "bash" }, call),
      event(
        "tool.finished",
        { status: "denied", duration_ms: 12, timing_source: "producer_monotonic" },
        call,
      ),
    ])
    expect(state.records["tool:c"]).toMatchObject({
      started_at: null,
      duration_ms: null,
      timing_source: null,
      status: "denied",
    })
  })

  it("keeps an explicitly unknown duration null without guessing from timestamps", () => {
    fresh()
    const req = { request_id: "r" }
    const state = replay([
      event("request.started", {}, req),
      event("request.finished", { status: "failed", duration_ms: null }, req),
    ])
    expect(state.records["request:r"]).toMatchObject({ duration_ms: null, timing_source: null })
  })

  it("keeps a missing duration null rather than zero", () => {
    fresh()
    const state = replay([event("user.x"), event("input.accepted", { text: "hi" }, { message_id: "m" })])
    expect(state.records["user:m"].duration_ms).toBeNull()
  })
})

describe("sequence numbers beyond Number.MAX_SAFE_INTEGER", () => {
  it("orders and de-duplicates by exact decimal value", () => {
    const base = { ...emptyState(), through_seq: "9007199254740992" }
    const make = (seq: string, output: string) => ({
      ...event("tool.output", { mode: "delta", output }, { call_id: "c" }),
      seq,
    })
    const started = {
      ...event("tool.requested", { name: "bash" }, { call_id: "c" }),
      seq: "9007199254740993",
    }
    const state = reduceMany(base, [
      started,
      make("9007199254740992", "stale"),
      make("9007199254740994", "x"),
    ])
    expect(state.through_seq).toBe("9007199254740994")
    expect(state.records["tool:c"].data.output).toBe("x")
    expect(state.records["tool:c"].start_seq).toBe("9007199254740993")
  })
})

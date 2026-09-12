import { describe, expect, it } from "vitest"
import type { ProjectionState, TrajectoryEvent } from "../types/protocol"
import { lineDiff, stableText } from "./diff"
import { nextStepSeq, playbackDelay, previousStepSeq, nextEventSeq, clockAt } from "./playback"
import { replay } from "./projector"
import { relationsFor } from "./relations"
import { lteSeq } from "./seq"
import { layoutTimeline } from "./timeline"

interface GoldenFixture {
  events: TrajectoryEvent[]
  expected_state: ProjectionState
}

const fixtureFiles = import.meta.glob<GoldenFixture>("../../../../../backend/trajectory/fixtures/*.json", {
  eager: true,
  import: "default",
})
const golden = Object.entries(fixtureFiles).find(([path]) => path.endsWith("/session_v1.json"))![1]
const EVENTS = golden.events
const at = (seq: string) => replay(EVENTS.filter((event) => lteSeq(event.seq, seq)))

describe("timeline layout", () => {
  it("keeps an open tool open at a historical position and closes it later", () => {
    const early = at("17")
    const open = layoutTimeline(Object.values(early.records), {
      scale: "sequence",
      headSeq: "17",
      clock: EVENTS[16].occurred_at,
    })
    expect(open.items.find((item) => item.recordId === "tool:call_a")).toMatchObject({ open: true, end: 1 })
    expect(open.items.some((item) => item.recordId === "question:q_a")).toBe(false)
    const late = layoutTimeline(Object.values(golden.expected_state.records), {
      scale: "sequence",
      headSeq: "34",
      clock: null,
    })
    expect(late.items.find((item) => item.recordId === "tool:call_a")?.open).toBe(false)
  })

  it("draws points as points and counts records without recorded time", () => {
    const records = Object.values(at("13").records)
    const layout = layoutTimeline(records, {
      scale: "duration",
      headSeq: "13",
      clock: EVENTS[12].occurred_at,
    })
    const user = layout.items.find((item) => item.recordId === "user:msg_user")!
    expect(user.point).toBe(true)
    expect(user.start).toBe(user.end)
    // The tool was requested but has not entered the executor: no start time yet.
    expect(layout.untimed).toBe(1)
    expect(layout.items.some((item) => item.recordId === "tool:call_a")).toBe(false)
  })
})

describe("replay navigation", () => {
  it("steps by event and by Step boundary", () => {
    expect(nextEventSeq("9", "34")).toBe("10")
    expect(nextEventSeq("34", "34")).toBeNull()
    expect(nextStepSeq(EVENTS, "1")).toBe("6")
    expect(nextStepSeq(EVENTS, "6")).toBeNull()
    expect(previousStepSeq(EVENTS, "20")).toBe("6")
  })

  it("paces by recorded time, treats clock skew as zero and caps idle waits", () => {
    const [a, b] = [EVENTS[0], EVENTS[1]]
    expect(playbackDelay(a, b, { rate: 1, skipIdle: false })).toBe(16)
    const late = { ...b, occurred_at: "2026-09-11T09:00:00.000Z" }
    expect(playbackDelay(a, late, { rate: 2, skipIdle: true })).toBe(200)
    const skewed = { ...b, occurred_at: "2026-09-11T07:00:00.000Z" }
    expect(playbackDelay(a, skewed, { rate: 1, skipIdle: false })).toBe(16)
    expect(clockAt(EVENTS, "17")).toBe(EVENTS[16].occurred_at)
  })
})

describe("relations", () => {
  const records = golden.expected_state.records

  it("links only to records that exist at the position", () => {
    const early = at("12").records
    // No agent record exists for the main agent, so no agent link is offered.
    expect(relationsFor(early["request:req_a"], early).map((link) => link.recordId)).toEqual([
      "assistant:req_a",
      "system:req_a",
      "turn:turn_a",
      "run:run_a",
    ])
    expect(early["question:q_a"]).toBeUndefined()
    const retry = relationsFor(records["retry:evt_fixture_28"], records).map((link) => link)
    expect(retry).toContainEqual({ kind: "nextAttempt", recordId: "request:req_retry" })
    expect(retry).toContainEqual({ kind: "request", recordId: "request:req_child" })
    expect(relationsFor(records["permission:perm_a"], records)).toContainEqual({
      kind: "tool",
      recordId: "tool:call_a",
    })
  })
})

describe("line diff", () => {
  it("marks added and removed lines", () => {
    expect(lineDiff("a\nb\nc", "a\nc\nd")).toEqual([
      { op: "same", text: "a" },
      { op: "del", text: "b" },
      { op: "same", text: "c" },
      { op: "add", text: "d" },
    ])
  })

  it("refuses inputs beyond the limit rather than guessing", () => {
    expect(lineDiff("a\nb", "a", 1)).toBeNull()
  })

  it("ignores key order in structured values", () => {
    expect(stableText({ b: 1, a: { d: 2, c: 3 } })).toBe(stableText({ a: { c: 3, d: 2 }, b: 1 }))
  })
})

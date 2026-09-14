import { describe, expect, it } from "vitest"
import { EventShapeError, ingestCheckpoint, validateEvent } from "./adapter"
import { emptyState } from "./projector"

const state = { ...emptyState(), through_seq: "10" }

describe("checkpoint ingestion", () => {
  it("reports that there is no checkpoint", () => {
    expect(ingestCheckpoint({ checkpoint: null, through_seq: "10" }, "10")).toEqual({ kind: "none" })
  })

  it("accepts a compatible checkpoint at or before the target", () => {
    const result = ingestCheckpoint(
      { checkpoint: { through_seq: "10", projector_version: 1, state }, through_seq: "10" },
      "12",
    )
    expect(result).toEqual({ kind: "ok", state })
  })

  it("rejects another projector version so the caller replays from zero", () => {
    const result = ingestCheckpoint(
      { checkpoint: { through_seq: "10", projector_version: 2, state }, through_seq: "10" },
      "12",
    )
    expect(result).toEqual({ kind: "rejected", reason: "incompatible_version" })
  })

  it("rejects a malformed or mislabelled state", () => {
    const broken = { ...state, records: { "tool:x": { record_id: "tool:y" } } }
    expect(
      ingestCheckpoint(
        { checkpoint: { through_seq: "10", projector_version: 1, state: broken }, through_seq: "10" },
        "12",
      ),
    ).toEqual({
      kind: "rejected",
      reason: "malformed",
    })
    expect(
      ingestCheckpoint(
        { checkpoint: { through_seq: "9", projector_version: 1, state }, through_seq: "9" },
        "12",
      ),
    ).toEqual({
      kind: "rejected",
      reason: "malformed",
    })
  })

  it("rejects a checkpoint whose records claim positions after its own watermark", () => {
    const record = {
      record_id: "tool:x",
      kind: "tool",
      title: "bash",
      start_seq: "4",
      as_of_seq: "11",
      end_seq: null,
      started_at: null,
      finished_at: null,
      duration_ms: null,
      data: {},
      blocks: [],
      usage: {},
      ...Object.fromEntries(
        [
          "source_session_id",
          "turn_id",
          "run_id",
          "generation",
          "agent_id",
          "parent_agent_id",
          "step_id",
          "request_id",
          "call_id",
          "parent_call_id",
          "message_id",
          "part_id",
          "caused_by_event_id",
        ].map((key) => [key, null]),
      ),
    }
    const future = { ...state, records: { "tool:x": record } }
    expect(
      ingestCheckpoint(
        { checkpoint: { through_seq: "10", projector_version: 1, state: future }, through_seq: "10" },
        "12",
      ),
    ).toEqual({
      kind: "rejected",
      reason: "future_watermark",
    })
    const finishedLater = { ...state, records: { "tool:x": { ...record, as_of_seq: "9", end_seq: "12" } } }
    expect(
      ingestCheckpoint(
        { checkpoint: { through_seq: "10", projector_version: 1, state: finishedLater }, through_seq: "10" },
        "12",
      ),
    ).toMatchObject({
      reason: "future_watermark",
    })
    const unsupportedLater = { ...state, unsupported_events: [{ seq: "11", type: "x.y", version: 2 }] }
    expect(
      ingestCheckpoint(
        {
          checkpoint: { through_seq: "10", projector_version: 1, state: unsupportedLater },
          through_seq: "10",
        },
        "12",
      ),
    ).toMatchObject({
      reason: "future_watermark",
    })
    const bounded = { ...state, records: { "tool:x": { ...record, as_of_seq: "9" } } }
    expect(
      ingestCheckpoint(
        { checkpoint: { through_seq: "10", projector_version: 1, state: bounded }, through_seq: "10" },
        "12",
      ).kind,
    ).toBe("ok")
  })

  it("rejects a checkpoint newer than the requested position", () => {
    const result = ingestCheckpoint(
      { checkpoint: { through_seq: "10", projector_version: 1, state }, through_seq: "10" },
      "9",
    )
    expect(result).toEqual({ kind: "rejected", reason: "beyond_target" })
  })
})

describe("event validation", () => {
  const good = {
    event_id: "e",
    seq: "1",
    type: "anything.new",
    version: 7,
    occurred_at: "2026-09-11T00:00:00Z",
    session_id: "s",
    user_id: "u",
    data: {},
  }

  it("lets unknown types and versions through for the projector to report", () => {
    expect(validateEvent(good)).toBe(good)
  })

  it("refuses envelopes it cannot place in the sequence", () => {
    expect(() => validateEvent({ ...good, seq: 1 })).toThrow(EventShapeError)
    expect(() => validateEvent({ ...good, data: "x" })).toThrow(EventShapeError)
    expect(() => validateEvent(null)).toThrow(EventShapeError)
  })
})

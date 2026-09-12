import { act, renderHook } from "@testing-library/react"
import { beforeEach, describe, expect, it } from "vitest"
import type { Position, SyncSnapshot } from "../../api/sync"
import { useTrajectoryView } from "../../stores/view"
import { PROJECTOR_VERSION, type TrajectoryEvent, type TrajectoryRecord } from "../../types/protocol"
import { hasSanitizedRaw } from "../inspector/panels/eventNotes"
import { makeRecord } from "../testing/harness"
import { sliderMax } from "./seekScale"
import { usePlaybackControls } from "./usePlaybackControls"

// Native perf shape: the head checkpoint sits at 100009, so the loaded event
// buffer starts there. Older history is only reachable through the engine.
const HEAD = "100012"

function event(seq: string, type: string): TrajectoryEvent {
  return {
    event_id: `evt_${seq}`,
    user_id: "owner",
    session_id: "ses",
    seq,
    type,
    version: 1,
    occurred_at: "2026-09-11T08:00:00.000Z",
    data: {},
  }
}

const SNAPSHOT: SyncSnapshot = {
  phase: "live",
  live: null,
  loadedSeq: HEAD,
  headSeq: HEAD,
  baseSeq: "100009",
  origin: "checkpoint",
  rejection: null,
  error: null,
  events: [
    event("100010", "request.started"),
    event("100011", "step.started"),
    event("100012", "request.finished"),
  ],
  version: 1,
}

function readyAt(seq: string, records: TrajectoryRecord[]): Position {
  return {
    status: "ready",
    seq,
    origin: "checkpoint",
    state: {
      projector_version: PROJECTOR_VERSION,
      through_seq: seq,
      records: Object.fromEntries(records.map((record) => [record.record_id, record])),
      unsupported_events: [],
      coverage_start: null,
    },
  }
}

const STEPS = [
  makeRecord({
    record_id: "step:s_17",
    kind: "step",
    step_id: "s_17",
    start_seq: "17",
    end_seq: "40",
    as_of_seq: HEAD,
  }),
  makeRecord({
    record_id: "step:s_75200",
    kind: "step",
    step_id: "s_75200",
    start_seq: "75200",
    end_seq: "75300",
    as_of_seq: HEAD,
  }),
  makeRecord({
    record_id: "step:s_100011",
    kind: "step",
    step_id: "s_100011",
    start_seq: "100011",
    end_seq: null,
    as_of_seq: HEAD,
  }),
]

beforeEach(() => {
  useTrajectoryView.getState().reset()
})

describe("usePlaybackControls with a head checkpoint", () => {
  it("offers the whole recorded history, not only the loaded buffer", () => {
    const { result } = renderHook(() => usePlaybackControls(SNAPSHOT, readyAt(HEAD, STEPS)))
    expect(result.current.model.floor).toBe("0")
    expect(sliderMax(result.current.model.floor, result.current.model.loadedSeq)).toBeGreaterThan(0)
    act(() => result.current.actions.seek("75200"))
    expect(useTrajectoryView.getState().playhead).toBe("75200")
    act(() => result.current.actions.seek("17"))
    expect(useTrajectoryView.getState().playhead).toBe("17")
    act(() => result.current.actions.seek("0"))
    expect(useTrajectoryView.getState().playhead).toBe("0")
  })

  it("steps back to Step starts before the loaded events using the position's Step records", () => {
    act(() => useTrajectoryView.getState().setPlayhead("100011"))
    const { result, rerender } = renderHook(({ position }) => usePlaybackControls(SNAPSHOT, position), {
      initialProps: { position: readyAt("100011", STEPS) },
    })
    expect(result.current.model.previousStep).toBe("75200")
    act(() => useTrajectoryView.getState().setPlayhead("75200"))
    rerender({ position: readyAt("75200", STEPS.slice(0, 2)) })
    expect(result.current.model.previousStep).toBe("17")
    expect(result.current.model.previousEvent).toBe("75199")
  })

  it("starts playback from the beginning of history when pressed at the head", () => {
    const { result } = renderHook(() => usePlaybackControls(SNAPSHOT, readyAt(HEAD, STEPS)))
    act(() => result.current.actions.togglePlay())
    expect(useTrajectoryView.getState().playhead).toBe("0")
    expect(useTrajectoryView.getState().playing).toBe(true)
  })
})

describe("next Step after a target-only seek", () => {
  function state(seq: string, records: TrajectoryRecord[]) {
    return (readyAt(seq, records) as Extract<Position, { status: "ready" }>).state
  }

  function snapshotWith(
    loadedSeq: string,
    liveRecords: TrajectoryRecord[],
    events: TrajectoryEvent[] = [],
  ): SyncSnapshot {
    return {
      ...SNAPSHOT,
      loadedSeq,
      headSeq: loadedSeq,
      baseSeq: loadedSeq,
      events,
      live: state(loadedSeq, liveRecords),
    }
  }

  const step = (start: string, id: string) =>
    makeRecord({
      record_id: `step:${id}`,
      kind: "step",
      step_id: id,
      start_seq: start,
      end_seq: null,
      as_of_seq: start,
    })

  it("finds the next Step from the loaded checkpoint's records when no events are loaded", () => {
    const live = [step("5", "a"), step("15", "b"), step("25", "c")]
    act(() => useTrajectoryView.getState().setPlayhead("10"))
    const { result } = renderHook(() =>
      usePlaybackControls(snapshotWith("30", live), readyAt("10", [live[0]])),
    )
    expect(result.current.model.nextStep).toBe("15")
    expect(result.current.model.previousStep).toBe("5")
    act(() => result.current.actions.seek(result.current.model.nextStep!))
    expect(useTrajectoryView.getState().playhead).toBe("15")
  })

  it("keeps the nearest boundary across loaded events and records, never past the loaded head", () => {
    const live = [step("5", "a"), step("25", "c"), step("40", "d")]
    act(() => useTrajectoryView.getState().setPlayhead("10"))
    const { result, rerender } = renderHook(
      ({ current }) => usePlaybackControls(current, readyAt("10", [])),
      {
        initialProps: { current: snapshotWith("30", live, [event("18", "step.started")]) },
      },
    )
    expect(result.current.model.nextStep).toBe("18")
    act(() => useTrajectoryView.getState().setPlayhead("25"))
    rerender({ current: snapshotWith("30", live, [event("18", "step.started")]) })
    expect(result.current.model.nextStep).toBeNull()
  })

  it("falls back to turn and request starts when nothing records Steps", () => {
    const live = [
      makeRecord({ record_id: "request:r1", kind: "request", request_id: "r1", start_seq: "7" }),
      makeRecord({ record_id: "request:r2", kind: "request", request_id: "r2", start_seq: "21" }),
    ]
    act(() => useTrajectoryView.getState().setPlayhead("10"))
    const { result } = renderHook(() =>
      usePlaybackControls(snapshotWith("30", live), readyAt("10", [live[0]])),
    )
    expect(result.current.model.nextStep).toBe("21")
    expect(result.current.model.previousStep).toBe("7")
  })

  it("compares very large decimal seqs exactly", () => {
    const base = "9007199254740993000"
    const live = [step(`${base}005`, "a"), step(`${base}015`, "b"), step(`${base}025`, "c")]
    act(() => useTrajectoryView.getState().setPlayhead(`${base}010`))
    const { result } = renderHook(() =>
      usePlaybackControls(snapshotWith(`${base}020`, live), readyAt(`${base}010`, [live[0]])),
    )
    expect(result.current.model.nextStep).toBe(`${base}015`)
    expect(result.current.model.previousStep).toBe(`${base}005`)
  })
})

describe("sanitized raw notes", () => {
  it("recognises both recorder raw-content modes", () => {
    const withMode = (mode: string) => [{ ...event("1", "request.delta"), data: { raw_content_mode: mode } }]
    expect(hasSanitizedRaw(withMode("sanitized_stream_references_and_complete_fields"))).toBe(true)
    expect(hasSanitizedRaw(withMode("sanitized_block_references"))).toBe(true)
    expect(hasSanitizedRaw(withMode("verbatim"))).toBe(false)
  })
})

import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { useSyncExternalStore } from "react"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { TrajectorySync, type SyncTransport } from "../api/sync"
import { useTrajectoryView } from "../stores/view"
import type { CheckpointResponse, EventPage, ProjectionState, TrajectoryEvent } from "../types/protocol"
import { replay } from "../utils/projector"
import { usePlaybackTimer, usePosition } from "./usePlayback"

interface GoldenFixture {
  events: TrajectoryEvent[]
}

const fixtures = import.meta.glob<GoldenFixture>("../../../../../backend/trajectory/fixtures/*.json", {
  eager: true,
  import: "default",
})
const EVENTS = Object.entries(fixtures).find(([path]) => path.endsWith("/session_v1.json"))![1].events
const HEAD = EVENTS[EVENTS.length - 1].seq
const LIVE_BASE: ProjectionState = replay(EVENTS.slice(0, 30))
const prefix = (seq: number) => replay(EVENTS.slice(0, seq))

/** Head checkpoint at 30, none older. Records the playhead at every bounded (history) event read. */
function fakeServer(opened: Promise<void> = Promise.resolve()) {
  const reads: Array<{ until: string; playhead: string | null }> = []
  const checkpoints: Array<string | undefined> = []
  const transport: SyncTransport = {
    async checkpoint(atSeq): Promise<CheckpointResponse> {
      checkpoints.push(atSeq)
      if (atSeq !== undefined) return { checkpoint: null, through_seq: atSeq }
      await opened
      return {
        checkpoint: { through_seq: "30", projector_version: 1, state: structuredClone(LIVE_BASE) },
        through_seq: HEAD,
      }
    },
    async events(params): Promise<EventPage> {
      if (params.untilSeq !== undefined)
        reads.push({ until: params.untilSeq, playhead: useTrajectoryView.getState().playhead })
      const until = params.untilSeq ?? HEAD
      const page = EVENTS.filter(
        (event) => BigInt(event.seq) > BigInt(params.afterSeq) && BigInt(event.seq) <= BigInt(until),
      )
      return {
        events: page,
        from_seq: page[0]?.seq ?? params.afterSeq,
        through_seq: page.at(-1)?.seq ?? params.afterSeq,
        until_seq: until,
        has_more: false,
        committed_seq: HEAD,
      }
    },
  }
  return { transport, reads, checkpoints }
}

function usePlayback(sync: TrajectorySync) {
  const snapshot = useSyncExternalStore(sync.subscribe, sync.getSnapshot)
  const position = usePosition(sync, snapshot)
  usePlaybackTimer(snapshot, position)
  return position
}

beforeEach(() => useTrajectoryView.getState().reset())
afterEach(cleanup)

describe("usePosition", () => {
  it("starts loading a pinned position once the head checkpoint is installed", async () => {
    let open!: () => void
    const server = fakeServer(new Promise<void>((resolve) => (open = resolve)))
    const sync = new TrajectorySync(server.transport)
    useTrajectoryView.setState({ playhead: "12" })
    const { result } = renderHook(() => usePlayback(sync))
    act(() => {
      void sync.open()
    })
    expect(result.current).toMatchObject({ status: "loading", seq: "12" })
    await act(async () => open())
    await waitFor(() =>
      expect(result.current).toMatchObject({ status: "ready", seq: "12", state: prefix(12) }),
    )
    expect(server.checkpoints).toEqual([undefined, "12"])
    sync.stop()
  })

  it("reads the next older event before playback moves onto it", async () => {
    const server = fakeServer()
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    useTrajectoryView.setState({ playhead: "12", rate: 8 })
    const { result } = renderHook(() => usePlayback(sync))
    await waitFor(() => expect(result.current?.status).toBe("ready"))
    act(() => useTrajectoryView.getState().setPlaying(true))
    // Plays through the live base (30) to the head and stops there.
    await waitFor(
      () => expect(useTrajectoryView.getState()).toMatchObject({ playhead: "34", playing: false }),
      { timeout: 5_000 },
    )
    const expected = [{ until: "12", playhead: "12" }]
    for (let seq = 13; seq < 30; seq += 1) expected.push({ until: String(seq), playhead: String(seq - 1) })
    expect(server.reads).toEqual(expected)
    expect(sync.stateAt("15")).toMatchObject({ status: "ready", state: prefix(15) })
    sync.stop()
  })
})

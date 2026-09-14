// Recovery behaviour of the catch-up engine against the shared backend
// fixture served by an in-memory stand-in for the events/checkpoint endpoints.
import { describe, expect, it, vi } from "vitest"
import { ApiError } from "@/shared/api/http"
import type { CheckpointResponse, EventPage, ProjectionState, TrajectoryEvent } from "../types/protocol"
import { emptyState, replay } from "../utils/projector"
import { TrajectorySync, type SyncTransport } from "./sync"
import type { EventPageParams } from "./endpoints"

interface GoldenFixture {
  events: TrajectoryEvent[]
  expected_state: ProjectionState
  historical: { through_seq: string; state: ProjectionState }
}

const fixtureFiles = import.meta.glob<GoldenFixture>("../../../../../backend/trajectory/fixtures/*.json", {
  eager: true,
  import: "default",
})
const golden = Object.entries(fixtureFiles).find(([path]) => path.endsWith("/session_v1.json"))![1]

const EVENTS = golden.events

interface ServerOptions {
  events?: TrajectoryEvent[]
  committed?: number
  checkpoints?: ProjectionState[]
  checkpointVersion?: number
  pageSize?: number
}

function fakeServer(options: ServerOptions = {}) {
  const events = options.events ?? EVENTS
  const state = { committed: options.committed ?? events.length }
  const calls: Array<
    { kind: "events"; after: string; until?: string } | { kind: "checkpoint"; at?: string }
  > = []
  const committedSeq = () => (state.committed === 0 ? "0" : events[state.committed - 1].seq)
  const transport: SyncTransport = {
    async events(params: EventPageParams): Promise<EventPage> {
      calls.push({ kind: "events", after: params.afterSeq, until: params.untilSeq })
      const until = params.untilSeq ?? committedSeq()
      const pending = events
        .slice(0, state.committed)
        .filter((event) => BigInt(event.seq) > BigInt(params.afterSeq) && BigInt(event.seq) <= BigInt(until))
      const limit = options.pageSize ?? params.limit ?? 500
      const page = pending.slice(0, limit)
      return {
        events: page,
        from_seq: page[0]?.seq ?? params.afterSeq,
        through_seq: page.at(-1)?.seq ?? params.afterSeq,
        until_seq: until,
        has_more: pending.length > limit,
        committed_seq: committedSeq(),
      }
    },
    async checkpoint(atSeq?: string): Promise<CheckpointResponse> {
      calls.push({ kind: "checkpoint", at: atSeq })
      const at = atSeq ?? committedSeq()
      const best = (options.checkpoints ?? [])
        .filter((cp) => BigInt(cp.through_seq) <= BigInt(at))
        .sort((a, b) => Number(BigInt(b.through_seq) - BigInt(a.through_seq)))[0]
      return {
        checkpoint: best
          ? {
              through_seq: best.through_seq,
              projector_version: options.checkpointVersion ?? 1,
              state: structuredClone(best),
            }
          : null,
        through_seq: at,
      }
    },
  }
  return { transport, calls, state }
}

const prefix = (seq: number) => replay(EVENTS.slice(0, seq))
const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, index) => from + index)
const seqs = (sync: TrajectorySync) => sync.getSnapshot().events.map((event) => Number(event.seq))

/** Bounded event reads (replay positions), as `after..until`. */
const historyReads = (server: ReturnType<typeof fakeServer>) =>
  server.calls.flatMap((call) =>
    call.kind === "events" && call.until !== undefined ? [`${call.after}..${call.until}`] : [],
  )
const checkpointReads = (server: ReturnType<typeof fakeServer>) =>
  server.calls.flatMap((call) => (call.kind === "checkpoint" && call.at !== undefined ? [call.at] : []))

async function seek(sync: TrajectorySync, seq: string) {
  sync.ensurePosition(seq)
  await vi.waitFor(() => expect(sync.stateAt(seq).status).not.toBe("loading"))
  return sync.stateAt(seq)
}

describe("opening a session", () => {
  it("replays from the start when there is no checkpoint", async () => {
    const server = fakeServer()
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    const snapshot = sync.getSnapshot()
    expect(snapshot.origin).toBe("start")
    expect(snapshot.loadedSeq).toBe("34")
    expect(snapshot.live).toEqual(golden.expected_state)
  })

  it("installs a compatible checkpoint and reads only the tail", async () => {
    const server = fakeServer({ checkpoints: [golden.historical.state] })
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    expect(sync.getSnapshot().origin).toBe("checkpoint")
    expect(server.calls).toContainEqual({ kind: "events", after: "17", until: undefined })
    expect(server.calls).not.toContainEqual({ kind: "events", after: "0", until: undefined })
    expect(sync.getSnapshot().live).toEqual(golden.expected_state)
  })

  it("falls back to a full replay when the checkpoint's projector is incompatible", async () => {
    const server = fakeServer({ checkpoints: [golden.historical.state], checkpointVersion: 2 })
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    const snapshot = sync.getSnapshot()
    expect(snapshot.origin).toBe("start_fallback")
    expect(snapshot.rejection).toBe("incompatible_version")
    expect(snapshot.live).toEqual(golden.expected_state)
  })

  it("follows cursor pages until the head", async () => {
    const server = fakeServer({ pageSize: 5 })
    const sync = new TrajectorySync(server.transport, { pageSize: 5 })
    await sync.open()
    expect(server.calls.filter((call) => call.kind === "events").length).toBeGreaterThanOrEqual(7)
    expect(sync.getSnapshot().live).toEqual(golden.expected_state)
  })
})

describe("live catch-up", () => {
  it("ignores duplicate and out-of-order watermark hints", async () => {
    const server = fakeServer()
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    const before = server.calls.length
    sync.noteCommitted("5")
    sync.noteCommitted("34")
    await Promise.resolve()
    expect(server.calls.length).toBe(before)
    expect(sync.getSnapshot().live).toEqual(golden.expected_state)
  })

  it("recovers when the final notification never arrives", async () => {
    const server = fakeServer({ committed: 20 })
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    expect(sync.getSnapshot().loadedSeq).toBe("20")
    server.state.committed = 34 // committed server-side, no notification sent
    await sync.poll()
    expect(sync.getSnapshot().loadedSeq).toBe("34")
    expect(sync.getSnapshot().live).toEqual(golden.expected_state)
  })

  it("drops a response that lands after the target was stopped", async () => {
    const server = fakeServer()
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    const slow: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async (params, signal) => {
        await gate
        return server.transport.events(params, signal)
      },
    }
    const sync = new TrajectorySync(slow)
    const opening = sync.open()
    await vi.waitFor(() => expect(sync.getSnapshot().phase).toBe("live"))
    sync.stop()
    release()
    await opening
    expect(sync.getSnapshot().phase).toBe("stopped")
    expect(sync.getSnapshot().loadedSeq).toBe("0")
  })

  it("reports a hole in the sequence instead of skipping it", async () => {
    const holed = EVENTS.filter((event) => event.seq !== "9")
    const server = fakeServer({ events: holed })
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    const snapshot = sync.getSnapshot()
    expect(snapshot.error).toEqual({ kind: "gap", seq: "9" })
    expect(snapshot.loadedSeq).toBe("0")
  })

  it("stops and reports when the viewer loses admin access", async () => {
    const onDenied = vi.fn()
    const server = fakeServer()
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async () => {
        throw new ApiError(403, "HTTP_403", "Forbidden")
      },
    }
    const sync = new TrajectorySync(transport, { onDenied })
    await sync.open()
    expect(sync.getSnapshot().phase).toBe("denied")
    expect(onDenied).toHaveBeenCalledWith({ kind: "denied", status: 403 })
    const calls = server.calls.length
    await sync.poll()
    expect(server.calls.length).toBe(calls)
  })
})

describe("lifecycle and clean-up", () => {
  it("forgets every loaded event and projection when stopped", async () => {
    const sync = new TrajectorySync(fakeServer().transport)
    await sync.open()
    expect(sync.stateAt("17").status).toBe("ready")
    sync.stop()
    const snapshot = sync.getSnapshot()
    expect(snapshot).toMatchObject({ phase: "stopped", live: null, loadedSeq: "0", headSeq: "0", events: [] })
    expect(sync.stateAt("17").status).toBe("loading")
  })

  it("wipes loaded content when access is denied mid-session", async () => {
    const server = fakeServer({ committed: 20 })
    let refuse = false
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async (params, signal) => {
        if (refuse) throw new ApiError(403, "HTTP_403", "Forbidden")
        return server.transport.events(params, signal)
      },
    }
    const onDenied = vi.fn()
    const sync = new TrajectorySync(transport, { onDenied })
    await sync.open()
    refuse = true
    await sync.poll()
    expect(sync.getSnapshot()).toMatchObject({ phase: "denied", live: null, events: [] })
    expect(onDenied).toHaveBeenCalledOnce()
  })

  it("drops everything when the session is deleted after it was shown, even without a new seq", async () => {
    const server = fakeServer()
    let deleted = false
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async (params, signal) => {
        if (deleted) throw new ApiError(410, "trajectory_content_deleted", "gone")
        return server.transport.events(params, signal)
      },
    }
    const onGone = vi.fn()
    const sync = new TrajectorySync(transport, { onGone })
    await sync.open()
    deleted = true
    await sync.poll()
    expect(sync.getSnapshot()).toMatchObject({
      phase: "gone",
      live: null,
      events: [],
      error: { kind: "deleted", status: 410 },
    })
    expect(onGone).toHaveBeenCalledOnce()
  })

  it("treats a 404 after loading as deletion, not as an unrecorded session", async () => {
    const server = fakeServer()
    let missing = false
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async (params, signal) => {
        if (missing) throw new ApiError(404, "HTTP_404", "Session not found")
        return server.transport.events(params, signal)
      },
    }
    const sync = new TrajectorySync(transport)
    await sync.open()
    missing = true
    await sync.poll()
    expect(sync.getSnapshot()).toMatchObject({ phase: "gone", live: null })
  })

  it("keeps the verified prefix and reports corruption", async () => {
    const server = fakeServer({ committed: 20 })
    let corrupt = false
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async (params, signal) => {
        if (corrupt) throw new ApiError(409, "trajectory_corrupt", "digest mismatch")
        return server.transport.events(params, signal)
      },
    }
    const sync = new TrajectorySync(transport)
    await sync.open()
    corrupt = true
    server.state.committed = 34
    await sync.poll()
    expect(sync.getSnapshot()).toMatchObject({
      phase: "live",
      loadedSeq: "20",
      error: { kind: "corrupt", status: 409 },
    })
    expect(sync.getSnapshot().live).toEqual(prefix(20))
  })

  it("retries an open that failed on the network from the next poll", async () => {
    const server = fakeServer()
    let offline = true
    const transport: SyncTransport = {
      events: server.transport.events,
      checkpoint: async (at, signal) => {
        if (offline) throw new TypeError("Failed to fetch")
        return server.transport.checkpoint(at, signal)
      },
    }
    const sync = new TrajectorySync(transport)
    await sync.open()
    expect(sync.getSnapshot()).toMatchObject({ phase: "opening", error: { kind: "network" } })
    offline = false
    await sync.poll()
    expect(sync.getSnapshot().live).toEqual(golden.expected_state)
  })

  it("rejects an event page that reaches past the watermark it was read at", async () => {
    const server = fakeServer({ committed: 10 })
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async (params, signal) => {
        const page = await server.transport.events(params, signal)
        return { ...page, events: [...page.events, EVENTS[10]] }
      },
    }
    const sync = new TrajectorySync(transport)
    await sync.open()
    expect(sync.getSnapshot()).toMatchObject({ loadedSeq: "0", error: { kind: "malformed", seq: "11" } })
  })
})

describe("replay positions", () => {
  it("lets only the most recent seek install its history", async () => {
    const checkpointAt8 = prefix(8)
    const server = fakeServer({ checkpoints: [checkpointAt8, golden.historical.state] })
    const gates = new Map<string, () => void>()
    const transport: SyncTransport = {
      events: server.transport.events,
      checkpoint: async (at, signal) => {
        if (at === "10") await new Promise<void>((resolve) => gates.set(at, resolve))
        return server.transport.checkpoint(at, signal)
      },
    }
    const sync = new TrajectorySync(transport)
    await sync.open()
    sync.ensurePosition("10") // slow: would install a segment starting at 8
    await vi.waitFor(() => expect(gates.has("10")).toBe(true))
    sync.ensurePosition("5") // latest seek: segment from the start
    await vi.waitFor(() => expect(sync.stateAt("5").status).toBe("ready"))
    gates.get("10")!()
    await new Promise((resolve) => setTimeout(resolve, 0))
    const at5 = sync.stateAt("5")
    expect(at5.status).toBe("ready")
    if (at5.status === "ready") expect(at5.state).toEqual(prefix(5))
  })

  it("builds historical positions from loaded events without future values", async () => {
    const sync = new TrajectorySync(fakeServer().transport)
    await sync.open()
    const at17 = sync.stateAt("17")
    expect(at17.status).toBe("ready")
    if (at17.status !== "ready") return
    expect(at17.state).toEqual(golden.historical.state)
    expect(at17.state.records["tool:call_a"].status).toBe("running")
    expect(at17.state.records["question:q_a"]).toBeUndefined()
  })

  it("keeps a historical position stable while the live head advances", async () => {
    const server = fakeServer({ committed: 25 })
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    const before = sync.stateAt("17")
    server.state.committed = 34
    await sync.poll()
    expect(sync.getSnapshot().loadedSeq).toBe("34")
    expect(sync.stateAt("17")).toEqual(before)
  })

  it("seeks before the installed checkpoint through checkpoint-or-start plus tail", async () => {
    const server = fakeServer({ checkpoints: [golden.historical.state] })
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    expect(sync.stateAt("10").status).toBe("loading")
    sync.ensurePosition("10")
    await vi.waitFor(() => expect(sync.stateAt("10").status).toBe("ready"))
    const at10 = sync.stateAt("10")
    if (at10.status !== "ready") return
    expect(at10.origin).toBe("start")
    expect(at10.state).toEqual(prefix(10))
    expect(server.calls).toContainEqual({ kind: "checkpoint", at: "10" })
    expect(server.calls).toContainEqual({ kind: "events", after: "0", until: "10" })
    // History between the position and the live base is not read.
    expect(seqs(sync)).toEqual([...range(1, 10), ...range(18, 34)])
  })

  it("clamps a position beyond what is loaded to the loaded head", async () => {
    const sync = new TrajectorySync(fakeServer({ committed: 12 }).transport)
    await sync.open()
    const position = sync.stateAt("30")
    expect(position.status === "ready" && position.seq).toBe("12")
  })
})

describe("seeking into history older than the live base", () => {
  // Live base 30 (head checkpoint), older checkpoint at 5.
  const CHECKPOINTS = [prefix(5), prefix(30)]

  it("reads events only through the target and shows it without the later history", async () => {
    const server = fakeServer({ checkpoints: CHECKPOINTS })
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      // History after position 12 never answers: showing 12 must not depend on it.
      events: (params, signal) =>
        params.untilSeq !== undefined && BigInt(params.untilSeq) > BigInt(12)
          ? new Promise<EventPage>(() => undefined)
          : server.transport.events(params, signal),
    }
    const sync = new TrajectorySync(transport)
    await sync.open()
    const at12 = await seek(sync, "12")
    expect(at12).toMatchObject({ status: "ready", seq: "12", origin: "checkpoint" })
    if (at12.status === "ready") expect(at12.state).toEqual(prefix(12))
    expect(historyReads(server)).toEqual(["5..12"])
    expect(seqs(sync)).toEqual([...range(6, 12), ...range(31, 34)])
  })

  it("extends the replay segment for short steps forward instead of refetching its checkpoint", async () => {
    const server = fakeServer({ checkpoints: CHECKPOINTS })
    const sync = new TrajectorySync(server.transport, { pageSize: 2 })
    await sync.open()
    for (const target of [12, 13, 14, 20]) {
      const position = await seek(sync, String(target))
      expect(position).toMatchObject({ status: "ready", origin: "checkpoint" })
      if (position.status === "ready") expect(position.state).toEqual(prefix(target))
    }
    expect(checkpointReads(server)).toEqual(["12"])
    expect(historyReads(server)).toEqual([
      "5..12",
      "7..12",
      "9..12",
      "11..12",
      "12..13",
      "13..14",
      "14..20",
      "16..20",
      "18..20",
    ])
    expect(sync.stateAt("13")).toMatchObject({ status: "ready", state: prefix(13) })

    // Beyond the extension window (4 pages of 2) and before the segment's base: a checkpoint read as usual.
    const at29 = await seek(sync, "29")
    if (at29.status === "ready") expect(at29.state).toEqual(prefix(29))
    const at3 = await seek(sync, "3")
    expect(at3).toMatchObject({ status: "ready", origin: "start", state: prefix(3) })
    expect(checkpointReads(server)).toEqual(["12", "29", "3"])
    expect(seqs(sync)).toEqual([...range(1, 3), ...range(31, 34)])
  })

  it("never lets a superseded read replace the position sought after it", async () => {
    const server = fakeServer({ checkpoints: CHECKPOINTS })
    let release: (() => void) | undefined
    const transport: SyncTransport = {
      checkpoint: server.transport.checkpoint,
      events: async (params, signal) => {
        if (params.untilSeq === "13") await new Promise<void>((resolve) => (release = resolve))
        return server.transport.events(params, signal)
      },
    }
    const sync = new TrajectorySync(transport)
    await sync.open()
    await seek(sync, "12")
    sync.ensurePosition("13") // extends the segment at 12; held
    await vi.waitFor(() => expect(release).toBeDefined())
    const at3 = await seek(sync, "3")
    release!()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(sync.stateAt("3")).toEqual(at3)
    expect(sync.stateAt("13").status).toBe("loading")
    expect(seqs(sync)).toEqual([...range(1, 3), ...range(31, 34)])
  })

  it("refuses a position whose history comes back short", async () => {
    const holed = EVENTS.filter((event) => event.seq !== "11" && event.seq !== "12")
    const sync = new TrajectorySync(fakeServer({ events: holed, checkpoints: CHECKPOINTS }).transport)
    await sync.open()
    expect(await seek(sync, "12")).toEqual({ status: "error", seq: "12", error: { kind: "gap", seq: "11" } })
    expect(sync.getSnapshot()).toMatchObject({ phase: "live", loadedSeq: "34", error: null })
  })
})

describe("sequence numbers beyond Number.MAX_SAFE_INTEGER", () => {
  it("catches up across the boundary exactly", async () => {
    const base = { ...emptyState(), through_seq: "9007199254740992" }
    const big = [EVENTS[12], EVENTS[15], EVENTS[16]].map((event, index) => ({
      ...event,
      seq: (BigInt("9007199254740993") + BigInt(index)).toString(),
    }))
    const server = fakeServer({ events: big, checkpoints: [base] })
    const sync = new TrajectorySync(server.transport)
    await sync.open()
    const snapshot = sync.getSnapshot()
    expect(snapshot.loadedSeq).toBe("9007199254740995")
    expect(snapshot.live?.records["tool:call_a"]).toMatchObject({
      start_seq: "9007199254740993",
      result_preview: "one",
    })
  })
})

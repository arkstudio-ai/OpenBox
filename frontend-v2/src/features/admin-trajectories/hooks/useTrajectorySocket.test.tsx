// Watermark hints: only the target's reach the engine and the header, a
// deletion is read at once, and a new hint callback never resubscribes.
import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { TrajectoryWatermark } from "@/shared/ws/events"
import type { TrajectorySync } from "../api/sync"
import { useTrajectoryAccess } from "../stores/access"
import { useTrajectorySocket } from "./useTrajectorySocket"

const socket = vi.hoisted(() => {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  return {
    handlers,
    sent: [] as Array<{ type: string }>,
    on(event: string, handler: (data: unknown) => void) {
      const set = handlers.get(event) ?? new Set()
      set.add(handler)
      handlers.set(event, set)
      return () => {
        set.delete(handler)
      }
    },
    emit(event: string, data: unknown) {
      for (const handler of handlers.get(event) ?? []) handler(data)
    },
    connect: async () => undefined,
    disconnect: () => undefined,
  }
})

vi.mock("../api/socket", () => ({
  trajectorySocket: socket,
  sendTrajectoryMessage: (message: { type: string }) => {
    socket.sent.push(message)
    return true
  },
}))

const HINT: TrajectoryWatermark = {
  user_id: "owner_a",
  owner_user_id: "owner_a",
  session_id: "ses_a",
  trajectory_id: "trj_a",
  committed_seq: "40",
}

function engine() {
  const sync = {
    noteCommitted: vi.fn(),
    poll: vi.fn(async () => undefined),
    getSnapshot: () => ({ loadedSeq: "34" }),
  }
  return { sync, asEngine: sync as unknown as TrajectorySync }
}

const subscriptions = () => socket.sent.filter((message) => message.type === "subscribe").length

beforeEach(() => {
  socket.handlers.clear()
  socket.sent.length = 0
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
})

afterEach(cleanup)

describe("watermark hints", () => {
  it("pass the target's commits to the engine and the header and ignore every other target", () => {
    const { sync, asEngine } = engine()
    const onHint = vi.fn()
    renderHook(() => useTrajectorySocket("ses_a", "owner_a", asEngine, onHint))
    expect(socket.sent).toContainEqual({ type: "subscribe", session_id: "ses_a", after_seq: "34" })

    act(() => socket.emit("trajectory.available", HINT))
    expect(sync.noteCommitted).toHaveBeenCalledWith("40")
    expect(onHint).toHaveBeenCalledWith(HINT)

    act(() => socket.emit("trajectory.available", { ...HINT, session_id: "ses_other" }))
    act(() => socket.emit("subscribed", { ...HINT, user_id: "owner_b", owner_user_id: "owner_b" }))
    expect(onHint).toHaveBeenCalledTimes(1)
    expect(sync.noteCommitted).toHaveBeenCalledTimes(1)

    act(() => socket.emit("subscribed", { ...HINT, committed_seq: "41" }))
    expect(sync.noteCommitted).toHaveBeenLastCalledWith("41")
    expect(onHint).toHaveBeenCalledTimes(2)
  })

  it("read at once when the target was deleted, which brings no newer seq", () => {
    const { sync, asEngine } = engine()
    const onHint = vi.fn()
    renderHook(() => useTrajectorySocket("ses_a", "owner_a", asEngine, onHint))
    act(() => socket.emit("trajectory.available", { ...HINT, deleted: true }))
    expect(sync.poll).toHaveBeenCalledTimes(1)
    expect(sync.noteCommitted).not.toHaveBeenCalled()
    expect(onHint).toHaveBeenCalledWith(expect.objectContaining({ deleted: true }))
  })

  it("reach the latest callback without subscribing again", () => {
    const { asEngine } = engine()
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = renderHook(
      ({ onHint }) => useTrajectorySocket("ses_a", "owner_a", asEngine, onHint),
      { initialProps: { onHint: first } },
    )
    const before = subscriptions()
    rerender({ onHint: second })
    act(() => socket.emit("trajectory.available", HINT))
    expect(second).toHaveBeenCalledWith(HINT)
    expect(first).not.toHaveBeenCalled()
    expect(subscriptions()).toBe(before)
  })
})

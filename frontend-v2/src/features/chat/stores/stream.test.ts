import { beforeEach, describe, expect, it } from "vitest"
import type { MessagePart, MessageWithParts } from "@/shared/types/api"
import { mergeSnapshotMessages, useStreamStore } from "./stream"

function message(parts: MessagePart[]): MessageWithParts {
  return {
    id: "message-1",
    session_id: "session-1",
    role: "assistant",
    created_at: "2026-08-25T00:00:00Z",
    parts,
  }
}

describe("mergeSnapshotMessages", () => {
  it("keeps a streamed text prefix when a delayed snapshot is shorter", () => {
    const live = message([{ id: "text-1", type: "text", text: "latest streamed answer" }])
    const stale = message([{ id: "text-1", type: "text", text: "latest" }])

    const merged = mergeSnapshotMessages([live], [stale])

    expect(merged[0].parts[0]).toMatchObject({ text: "latest streamed answer" })
  })

  it("uses a newer durable checkpoint and never regresses a terminal tool", () => {
    const live = message([
      { id: "reason-1", type: "reasoning", text: "short" },
      { id: "tool-1", type: "tool", tool: "bash", status: "completed", output: "done" },
    ])
    const snapshot = message([
      { id: "reason-1", type: "reasoning", text: "a much newer durable checkpoint" },
      { id: "tool-1", type: "tool", tool: "bash", status: "running" },
    ])

    const merged = mergeSnapshotMessages([live], [snapshot])

    expect(merged[0].parts[0]).toMatchObject({ text: "a much newer durable checkpoint" })
    expect(merged[0].parts[1]).toMatchObject({ status: "completed", output: "done" })
  })

  it("retains a WS message or part that landed after the snapshot SELECT", () => {
    const live = [
      message([
        { id: "text-1", type: "text", text: "answer" },
        { id: "tool-2", type: "tool", tool: "read", status: "running" },
      ]),
      { ...message([]), id: "message-2" },
    ]
    const snapshot = [message([{ id: "text-1", type: "text", text: "answer" }])]

    const merged = mergeSnapshotMessages(live, snapshot)

    expect(merged[0].parts.map((part) => part.id)).toEqual(["text-1", "tool-2"])
    expect(merged.map((item) => item.id)).toEqual(["message-1", "message-2"])
  })
})

describe("generation-aware events", () => {
  beforeEach(() => {
    useStreamStore.setState({
      status: new Map(),
      statusGeneration: new Map(),
      terminalStatusGeneration: new Map(),
      retry: new Map(),
      runError: new Map(),
    })
  })

  it("does not let an old terminal event replace a newer busy generation", () => {
    const stream = useStreamStore.getState()

    expect(stream.applyStatusEvent("session-1", "busy", 2)).toBe(true)
    expect(stream.applyStatusEvent("session-1", "idle", 1)).toBe(false)
    expect(stream.applyStatusEvent("session-1", "error", undefined)).toBe(false)
    expect(useStreamStore.getState().status.get("session-1")).toBe("busy")
  })

  it("rejects a delayed old transcript event after a newer event watermark", () => {
    const stream = useStreamStore.getState()

    expect(stream.acceptEventGeneration("session-1", 3)).toBe(true)
    expect(stream.acceptEventGeneration("session-1", 2)).toBe(false)
    expect(stream.acceptEventGeneration("session-1", undefined)).toBe(true)
  })

  it("keeps a terminal status closed within the same generation", () => {
    const stream = useStreamStore.getState()

    expect(stream.applyStatusEvent("session-1", "finalizing", 4)).toBe(true)
    expect(stream.applyStatusEvent("session-1", "idle", 4)).toBe(true)
    expect(stream.applyStatusEvent("session-1", "finalizing", 4)).toBe(false)
    expect(useStreamStore.getState().status.get("session-1")).toBe("idle")
  })
})

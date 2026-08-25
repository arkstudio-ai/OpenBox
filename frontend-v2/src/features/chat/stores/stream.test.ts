import { describe, expect, it } from "vitest"
import type { MessagePart, MessageWithParts } from "@/shared/types/api"
import { mergeSnapshotMessages } from "./stream"

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

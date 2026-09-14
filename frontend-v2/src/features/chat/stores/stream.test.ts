import { describe, expect, it } from "vitest"
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

describe("history pages", () => {
  const msg = (id: string, text = id, extra: Partial<MessageWithParts> = {}): MessageWithParts => ({
    id,
    session_id: "s1",
    role: "assistant",
    created_at: id,
    parts: [{ id: `${id}-text`, type: "text", text }],
    ...extra,
  })
  const held = () => useStreamStore.getState().messages.get("s1")?.map((m) => m.id)
  const paging = () => useStreamStore.getState().history.get("s1")

  it("keeps older pages the reader loaded when the newest turns are refreshed", () => {
    const older = [msg("m1"), msg("m2")]

    const merged = mergeSnapshotMessages([...older, msg("m3"), msg("m4")], [msg("m3"), msg("m4", "m4 done"), msg("m5")])

    expect(merged.map((m) => m.id)).toEqual(["m1", "m2", "m3", "m4", "m5"])
    expect(merged[0]).toBe(older[0])
    expect(merged[1]).toBe(older[1])
    expect(merged[3].parts[0]).toMatchObject({ text: "m4 done" })
  })

  it("keeps an unchanged message's object so its row can skip rendering", () => {
    const list = [msg("m1"), msg("m2")]

    const merged = mergeSnapshotMessages(list, [msg("m2")])

    expect(merged[1]).toBe(list[1])
  })

  it("drops a message deleted inside the page but keeps a send the server has not confirmed", () => {
    const pending = msg("tmp-1", "hi", { role: "user", client_message_id: "c1" })

    const merged = mergeSnapshotMessages([msg("m1"), msg("m2"), pending, msg("m3")], [msg("m1"), msg("m3")])

    expect(merged.map((m) => m.id)).toEqual(["m1", "m3", "tmp-1"])
  })

  it("replaces an optimistic send once the page carries its confirmed copy", () => {
    const pending = msg("tmp-1", "hi", { role: "user", client_message_id: "c1" })
    const confirmed = msg("m2", "hi", { role: "user", client_message_id: "c1" })

    const merged = mergeSnapshotMessages([msg("m1"), pending], [msg("m1"), confirmed])

    expect(merged.map((m) => m.id)).toEqual(["m1", "m2"])
  })

  it("keeps WS messages newer than a page that shares nothing with them", () => {
    const merged = mergeSnapshotMessages([msg("m9")], [msg("m7"), msg("m8")])

    expect(merged.map((m) => m.id)).toEqual(["m7", "m8", "m9"])
  })

  it("drops held turns a newest page no longer reaches instead of showing a silent gap", () => {
    const store = useStreamStore.getState()
    store.clearMessages("s1")
    store.mergeHistory("s1", [msg("m1"), msg("m2")], false)

    store.mergeHistory("s1", [msg("m7"), msg("m8")], true)

    expect(held()).toEqual(["m7", "m8"])
    expect(paging()?.hasMore).toBe(true)
    store.clearMessages("s1")
  })

  it("takes has_more only from a page that reaches the top of what is held", () => {
    const store = useStreamStore.getState()
    store.clearMessages("s1")
    store.mergeHistory("s1", [msg("m3"), msg("m4")], true)
    expect(paging()).toEqual({ hasMore: true, loadingOlder: false })

    store.prependHistory("s1", "m3", [msg("m1"), msg("m2")], false)
    expect(held()).toEqual(["m1", "m2", "m3", "m4"])
    expect(paging()?.hasMore).toBe(false)

    // The newest turns still have older turns above them, but those are loaded.
    store.mergeHistory("s1", [msg("m3"), msg("m4"), msg("m5")], true)
    expect(held()).toEqual(["m1", "m2", "m3", "m4", "m5"])
    expect(paging()?.hasMore).toBe(false)
    store.clearMessages("s1")
    expect(paging()).toBeUndefined()
  })

  it("ignores an older page that lands after the view was reset", () => {
    const store = useStreamStore.getState()
    store.clearMessages("s1")
    store.mergeHistory("s1", [msg("m3")], true)
    store.setHistoryLoading("s1", true)
    store.clearMessages("s1")
    store.mergeHistory("s1", [msg("m7")], true)

    store.prependHistory("s1", "m3", [msg("m1")], false)

    expect(held()).toEqual(["m7"])
    expect(paging()).toEqual({ hasMore: true, loadingOlder: false })
    store.clearMessages("s1")
  })
})

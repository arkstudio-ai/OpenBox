import { describe, expect, it } from "vitest"
import type { MessageWithParts, SessionStatus, SuggestionsPart } from "@/shared/types/api"
import { latestSuggestions } from "./suggestions"
import { mergeTurns } from "./turn-view"
import { useStreamStore } from "../stores/stream"

const part: SuggestionsPart = { type: "suggestions", id: "p1", items: [
  { label: "精简开头", prompt: "把开头精简到三句话。", mode: "send" },
] }
const user: MessageWithParts = { id: "u1", session_id: "s1", role: "user", created_at: "1", parts: [
  { type: "text", id: "t1", text: "优化文案" },
] }
const answer: MessageWithParts = { id: "a1", session_id: "s1", role: "assistant", created_at: "2", finish: "stop", parts: [part] }

describe("suggestions belong only to the latest successful answer", () => {
  it("restores cached chips from the message snapshot", () => {
    expect(latestSuggestions(mergeTurns([user, answer]), "idle")).toEqual(part)
  })

  it.each<SessionStatus | undefined>([undefined, "busy", "retry", "compacting", "waiting_input", "queued", "error"])(
    "hides suggestions in state %s", (status) => {
      expect(latestSuggestions(mergeTurns([user, answer]), status)).toBeUndefined()
    },
  )

  it.each([{ atBottom: false }, { readOnly: true }, { hasError: true }, { permissionCount: 1 }, { questionCount: 1 }])(
    "hides when blocked by %s", (visibility) => {
      expect(latestSuggestions(mergeTurns([user, answer]), "idle", visibility)).toBeUndefined()
    },
  )

  it.each(["aborted", "waiting_input", "tool_calls", undefined])("hides a %s answer", (finish) => {
    expect(latestSuggestions(mergeTurns([user, { ...answer, finish }]), "idle")).toBeUndefined()
  })

  it("does not inherit a previous message's chips or erase a turn error", () => {
    const next = { ...answer, id: "a2", parts: [] }
    expect(latestSuggestions(mergeTurns([user, answer, next]), "idle")).toBeUndefined()
    expect(latestSuggestions(mergeTurns([user, { ...answer, error: { message: "failed" } }]), "idle")).toBeUndefined()
  })

  it("ignores a late part event after the next user message", () => {
    const store = useStreamStore.getState()
    store.clearMessages("s1")
    store.setMessages("s1", [user, { ...answer, parts: [] }])
    store.addMessage("s1", { ...user, id: "u2", created_at: "3" })
    store.addPart("s1", "a1", part)
    expect(latestSuggestions(mergeTurns(useStreamStore.getState().messages.get("s1")!), "idle")).toBeUndefined()
    store.clearMessages("s1")
  })

  it("accepts an async part event without losing it to an older snapshot", () => {
    const store = useStreamStore.getState()
    const snapshot = [user, { ...answer, parts: [] }]
    store.clearMessages("s1")
    store.setMessages("s1", snapshot)
    store.addPart("s1", "a1", part)
    store.setMessages("s1", snapshot)
    expect(latestSuggestions(mergeTurns(useStreamStore.getState().messages.get("s1")!), "idle")).toEqual(part)
    store.clearMessages("s1")
  })
})

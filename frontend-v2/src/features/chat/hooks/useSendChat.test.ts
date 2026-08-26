/**
 * A rejected send must not look like a successful one.
 *
 * The composer cleared the draft the moment submit ran, the optimistic echo
 * stayed in the transcript, and the failure never reached either — so hitting
 * the session quota emptied the box, left the message on screen, and said
 * nothing. These pin the three halves of that.
 */
import { describe, expect, it } from "vitest"
import { useStreamStore } from "@/features/chat/stores/stream"
import type { MessageWithParts } from "@/shared/types/api"

function optimistic(sessionId: string, clientId: string): MessageWithParts {
  return {
    id: `tmp-${clientId}`,
    session_id: sessionId,
    role: "user",
    client_message_id: clientId,
    parts: [{ id: "p1", type: "text", text: "hello" }],
  } as unknown as MessageWithParts
}

describe("dropOptimistic", () => {
  it("removes the temp echo for a send that was rejected", () => {
    const store = useStreamStore.getState()
    store.addMessage("s1", optimistic("s1", "c1"))
    expect(useStreamStore.getState().messages.get("s1")).toHaveLength(1)

    useStreamStore.getState().dropOptimistic("s1", "c1")
    expect(useStreamStore.getState().messages.get("s1") ?? []).toHaveLength(0)
  })

  it("leaves other pending messages alone", () => {
    const store = useStreamStore.getState()
    store.addMessage("s2", optimistic("s2", "keep"))
    store.addMessage("s2", optimistic("s2", "drop"))

    useStreamStore.getState().dropOptimistic("s2", "drop")
    const left = useStreamStore.getState().messages.get("s2") ?? []
    expect(left.map((m) => m.client_message_id)).toEqual(["keep"])
  })

  it("never removes a server-confirmed message", () => {
    // A slow success must not erase itself: only ids starting tmp- are temp.
    const confirmed = {
      ...optimistic("s3", "c3"),
      id: "msg_real",
    } as MessageWithParts
    useStreamStore.getState().addMessage("s3", confirmed)

    useStreamStore.getState().dropOptimistic("s3", "c3")
    expect(useStreamStore.getState().messages.get("s3")).toHaveLength(1)
  })

  it("is a no-op for a session it has never seen", () => {
    expect(() => useStreamStore.getState().dropOptimistic("nope", "x")).not.toThrow()
  })
})

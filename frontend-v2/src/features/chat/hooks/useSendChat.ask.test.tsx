import { act, renderHook } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { QuestionRequest } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { useStreamStore } from "../stores/stream"
import { useSendChat } from "./useSendChat"
import { useSendReceiptStore } from "../stores/send-receipts"
import { optimisticUserMessage } from "../lib/message"

const send = vi.hoisted(() => vi.fn())
vi.mock("../api/messages", () => ({ useSendMessage: () => ({ mutateAsync: send }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Failed" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const old: QuestionRequest = { id: "old", session_id: "s1", questions: [{ question: "Old?", options: [] }] }
beforeEach(() => {
  send.mockReset()
  usePendingStore.setState({ questions: new Map([["s1", [old]]]), closedQuestions: new Set() })
  useStreamStore.setState({ messages: new Map(), status: new Map([["s1", "waiting_input"]]),
    statusRevision: new Map(), statusGeneration: new Map(), terminalStatusGeneration: new Map() })
})

describe("replacement-message ask semantics", () => {
  it("keeps the old ask when sending fails", async () => {
    send.mockRejectedValue(new Error("offline"))
    const { result } = renderHook(() => useSendChat("s1"))
    await act(async () => { await expect(result.current("Changed requirements")).rejects.toThrow("offline") })
    expect(usePendingStore.getState().questions.get("s1")).toEqual([old])
    expect(useStreamStore.getState().status.get("s1")).toBe("waiting_input")
  })

  it("removes only old asks after successful send, whether or not a new ask arrives", async () => {
    const newer = { ...old, id: "new" }
    send.mockImplementation(async () => { usePendingStore.getState().addQuestion(newer) })
    const { result } = renderHook(() => useSendChat("s1"))
    await act(async () => { await result.current("Changed requirements") })
    expect(usePendingStore.getState().questions.get("s1")).toEqual([newer])
    expect(usePendingStore.getState().closedQuestions.has("old")).toBe(true)
  })

  it.each(["busy", "idle", "error"] as const)("does not roll back a newer server %s after a delayed HTTP error", async (status) => {
    send.mockImplementation(async () => {
      useStreamStore.getState().applyStatusEvent("s1", status, 9)
      throw new TypeError("late transport failure")
    })
    const { result } = renderHook(() => useSendChat("s1"))
    await act(async () => { await expect(result.current(`Server ${status} before HTTP`)).rejects.toThrow() })
    expect(useStreamStore.getState().status.get("s1")).toBe(status)
    expect(useStreamStore.getState().statusGeneration.get("s1")).toBe(9)
  })

  it("rolls back its own status even when an unrelated session changed", async () => {
    send.mockImplementation(async () => {
      useStreamStore.getState().applyStatusEvent("unrelated", "busy", 9)
      throw new TypeError("offline")
    })
    const { result } = renderHook(() => useSendChat("s1"))
    await act(async () => { await expect(result.current("Unrelated session event")).rejects.toThrow() })
    expect(useStreamStore.getState().status.get("s1")).toBe("waiting_input")
  })

  it("accepts a durable WS echo despite a later HTTP failure without restoring the draft", async () => {
    send.mockImplementation(async ({ text, clientMessageId }) => {
      useStreamStore.getState().addMessage("s1", { ...optimisticUserMessage("s1", text, clientMessageId), id: "message-durable" })
      useStreamStore.getState().applyStatusEvent("s1", "idle", 10)
      throw new TypeError("response lost")
    })
    const { result } = renderHook(() => useSendChat("s1"))
    await act(async () => { await expect(result.current("Durably accepted before HTTP failure")).resolves.toBeUndefined() })
    const id = send.mock.calls[0][0].clientMessageId
    expect(useSendReceiptStore.getState().receipts.get(`s1:${id}`)).toBe("accepted")
    expect(useStreamStore.getState().status.get("s1")).toBe("idle")
    expect(usePendingStore.getState().questions.get("s1")).toEqual([old])
  })

  it("replays an uncertain send with the same key and keeps a queued task's pending question", async () => {
    send.mockRejectedValueOnce(new TypeError("network lost"))
      .mockResolvedValue({ delivery: "followup", state: "accepted", inbox_id: "durable" })
    const { result } = renderHook(() => useSendChat("s1"))
    await act(async () => { await expect(result.current("Followup after timeout", { attachments: ["a", "b"] })).rejects.toThrow() })
    const id = send.mock.calls[0][0].clientMessageId
    expect(useSendReceiptStore.getState().receipts.get(`s1:${id}`)).toBe("uncertain")
    await act(async () => { await result.current("Followup after timeout", { attachments: ["a", "b"] }) })
    expect(send.mock.calls[1][0].clientMessageId).toBe(id)
    expect(usePendingStore.getState().questions.get("s1")).toEqual([old])
    expect(useSendReceiptStore.getState().receipts.get(`s1:${id}`)).toBe("accepted")
    await act(async () => { await result.current("Followup after timeout", { attachments: ["a", "b"] }) })
    expect(send.mock.calls[2][0].clientMessageId).not.toBe(id)
  })
})

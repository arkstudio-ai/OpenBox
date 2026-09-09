import { act, renderHook } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { QuestionRequest } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { useStreamStore } from "../stores/stream"
import { useSendChat } from "./useSendChat"

const send = vi.hoisted(() => vi.fn())
vi.mock("../api/messages", () => ({ useSendMessage: () => ({ mutateAsync: send }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Failed" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const old: QuestionRequest = { id: "old", session_id: "s1", questions: [{ question: "Old?", options: [] }] }
beforeEach(() => {
  send.mockReset()
  usePendingStore.setState({ questions: new Map([["s1", [old]]]), closedQuestions: new Set() })
  useStreamStore.setState({ messages: new Map(), status: new Map([["s1", "waiting_input"]]) })
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
})

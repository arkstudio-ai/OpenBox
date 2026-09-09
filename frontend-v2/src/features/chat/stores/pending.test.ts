import { beforeEach, describe, expect, it } from "vitest"
import { usePendingStore } from "./pending"
import type { QuestionRequest } from "@/shared/types/api"

const question: QuestionRequest = { id: "q", session_id: "s", questions: [{ question: "?" }], draft_revision: 1 }
beforeEach(() => usePendingStore.setState({ questions: new Map(), closedQuestions: new Set() }))

describe("durable question events", () => {
  it("updates a saved draft but ignores duplicate and older snapshots", () => {
    const store = usePendingStore.getState()
    store.addQuestion(question)
    store.addQuestion({ ...question, draft_revision: 2 })
    store.addQuestion(question)
    store.setQuestions([question])
    expect(usePendingStore.getState().questions.get("s")).toHaveLength(1)
    expect(usePendingStore.getState().questions.get("s")?.[0].draft_revision).toBe(2)
  })

  it("does not revive a resolved question when asked and list events arrive late", () => {
    const store = usePendingStore.getState()
    store.removeQuestion("q")
    store.addQuestion(question)
    store.setQuestions([question])
    expect(usePendingStore.getState().questions.get("s") ?? []).toEqual([])
  })

  it("allows independent questions in one turn while filtering terminal state", () => {
    const store = usePendingStore.getState()
    store.addQuestion(question)
    store.addQuestion({ ...question, id: "another" })
    store.addQuestion({ ...question, id: "old", status: "superseded" })
    expect(usePendingStore.getState().questions.get("s")).toHaveLength(2)
  })
})

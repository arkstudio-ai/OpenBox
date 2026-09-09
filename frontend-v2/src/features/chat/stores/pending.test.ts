import { beforeEach, describe, expect, it } from "vitest"
import { usePendingStore } from "./pending"
import type { QuestionRequest } from "@/shared/types/api"

const question: QuestionRequest = {
  id: "q",
  session_id: "s",
  questions: [{ question: "?" }],
  draft_revision: 1,
}
beforeEach(() => usePendingStore.getState().reset())

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

  it("keeps an ask that arrived over WS after a pending snapshot started", () => {
    const store = usePendingStore.getState()
    const read = store.beginQuestionRead()
    store.addQuestion(question)
    store.setQuestions([], read)
    expect(usePendingStore.getState().questions.get("s")?.[0].id).toBe("q")
  })

  it("does not let an older HTTP read overwrite a newer completed read", () => {
    const store = usePendingStore.getState()
    const old = store.beginQuestionRead()
    const fresh = store.beginQuestionRead()
    store.setQuestions([question], fresh)
    store.setQuestions([], old)
    expect(usePendingStore.getState().questions.get("s")?.[0].id).toBe("q")
  })

  it("removes offline-resolved asks on a fresh read and rejects their late events", () => {
    const store = usePendingStore.getState()
    store.addQuestion(question)
    store.setQuestions([], store.beginQuestionRead())
    store.addQuestion(question)
    expect(usePendingStore.getState().questions.get("s") ?? []).toEqual([])
  })

  it("does not revive an answer that arrived while the snapshot was in flight", () => {
    const store = usePendingStore.getState()
    store.addQuestion(question)
    const read = store.beginQuestionRead()
    store.removeQuestion(question.id)
    store.setQuestions([question], read)
    expect(usePendingStore.getState().questions.get("s") ?? []).toEqual([])
  })

  it("ignores reads from an earlier account epoch", () => {
    const store = usePendingStore.getState()
    const oldAccountRead = store.beginQuestionRead()
    store.reset()
    store.setQuestions([question], oldAccountRead)
    expect(usePendingStore.getState().questions.size).toBe(0)
  })
})

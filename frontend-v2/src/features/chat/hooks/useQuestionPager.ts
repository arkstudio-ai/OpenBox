import { useState } from "react"
import { questionDraftKey } from "../api/question"
import { useUserId } from "../api/messages"

/** Navigation is local UI state, never an answer or a server draft revision. */
export function useQuestionPager(requestId: string, answers: string[][]) {
  const userId = useUserId()
  const key = `${questionDraftKey(userId, requestId)}:page`
  const last = Math.max(0, answers.length - 1)
  const [saved, setSaved] = useState(() => {
    const missing = answers.findIndex((answer) => answer.length === 0)
    let page = missing < 0 ? last : missing
    try {
      const value: unknown = JSON.parse(localStorage.getItem(key) ?? "null")
      if (typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= last) page = value
    } catch {
      /* A corrupt page preference cannot hide the question. */
    }
    return { key, page }
  })
  // The request/account boundary normally remounts the card; also fence a
  // direct prop change so the previous question's page is never borrowed.
  const page = saved.key === key ? Math.min(saved.page, last) : 0
  const goTo = (next: number) => {
    const bounded = Math.max(0, Math.min(next, last))
    setSaved({ key, page: bounded })
    try {
      localStorage.setItem(key, JSON.stringify(bounded))
    } catch {
      /* Private browsing. */
    }
  }
  return { page, goTo }
}

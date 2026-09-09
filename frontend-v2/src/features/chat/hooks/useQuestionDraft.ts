import { useCallback, useEffect, useRef, useState } from "react"
import type { QuestionDraftAnswer, QuestionRequest } from "@/shared/types/api"
import { ApiError } from "@/shared/api/http"
import { clearQuestionDraft, getQuestion, questionDraftKey, saveQuestionDraft } from "../api/question"
import { usePendingStore } from "../stores/pending"
import { useUserId } from "../api/messages"

export function initialQuestionDraft(request: QuestionRequest, userId: string): QuestionDraftAnswer[] {
  const server = request.questions.map((_, i) => request.draft?.[i] ?? { selected: [], custom: "", use_custom: false })
  try {
    const cached = JSON.parse(localStorage.getItem(questionDraftKey(userId, request.id)) ?? "null")
    if (cached?.revision === (request.draft_revision ?? 0) && Array.isArray(cached.draft)
      && cached.draft.length === server.length && cached.draft.every((item: QuestionDraftAnswer) =>
        Array.isArray(item.selected) && item.selected.every((v) => typeof v === "string")
        && typeof item.custom === "string" && typeof item.use_custom === "boolean")) return cached.draft
  } catch { /* A corrupt cache must not hide the server's saved answers. */ }
  return server
}

export function questionAnswers(draft: QuestionDraftAnswer[]): string[][] {
  return draft.map((item) => item.use_custom ? (item.custom.trim() ? [item.custom.trim()] : []) : item.selected)
}

export function useQuestionDraft(request: QuestionRequest, disabled: boolean) {
  const userId = useUserId()
  const [draft, setDraft] = useState(() => initialQuestionDraft(request, userId))
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<"conflict" | "failed" | null>(null)
  const [attempt, setAttempt] = useState(0)
  const revision = useRef(request.draft_revision ?? 0)
  const saved = useRef(JSON.stringify(request.questions.map((_, i) =>
    request.draft?.[i] ?? { selected: [], custom: "", use_custom: false })))
  const inFlight = useRef(false)
  const liveDraft = useRef(draft)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const cache = useCallback((value: QuestionDraftAnswer[], baseRevision: number) => {
    try { localStorage.setItem(questionDraftKey(userId, request.id), JSON.stringify({ revision: baseRevision, draft: value })) }
    catch { /* Server autosave remains available if storage is disabled. */ }
  }, [userId, request.id])

  const update = useCallback((index: number, value: QuestionDraftAnswer) => {
    const next = liveDraft.current.map((item, i) => i === index ? value : item)
    liveDraft.current = next
    setDraft(next)
    setSaveError(null)
    cache(next, revision.current)
  }, [cache])

  useEffect(() => {
    const nextRevision = request.draft_revision ?? 0
    if (nextRevision <= revision.current || inFlight.current
      || JSON.stringify(liveDraft.current) !== saved.current) return
    const next = request.questions.map((_, i) => request.draft?.[i]
      ?? { selected: [], custom: "", use_custom: false })
    revision.current = nextRevision
    saved.current = JSON.stringify(next)
    liveDraft.current = next
    setDraft(next)
    cache(next, nextRevision)
  }, [request, cache, attempt])

  useEffect(() => {
    if (disabled || inFlight.current || saveError || JSON.stringify(draft) === saved.current) return
    const timer = setTimeout(() => {
      const snapshot = liveDraft.current
      inFlight.current = true
      setSaving(true)
      void saveQuestionDraft(request.id, snapshot, revision.current).then((response) => {
        revision.current = response.draft_revision ?? revision.current + 1
        saved.current = JSON.stringify(snapshot)
        if (mounted.current) cache(liveDraft.current, revision.current)
      }).catch((error: unknown) => {
        if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
          usePendingStore.getState().removeQuestion(request.id)
          clearQuestionDraft(userId, request.id)
        } else if (mounted.current) setSaveError(error instanceof ApiError && error.status === 409 ? "conflict" : "failed")
      }).finally(() => {
        inFlight.current = false
        if (mounted.current) {
          setSaving(false)
          setAttempt((n) => n + 1)
        }
      })
    }, 400)
    return () => clearTimeout(timer)
  }, [draft, disabled, saveError, attempt, request.id, cache, userId])

  const retrySave = async () => {
    if (saveError === "conflict") {
      try {
        const latest = await getQuestion(request.id)
        if (latest.status && latest.status !== "pending") {
          usePendingStore.getState().removeQuestion(request.id)
          clearQuestionDraft(userId, request.id)
          return
        }
        revision.current = latest.draft_revision ?? revision.current
      } catch { return }
    }
    setSaveError(null)
    setAttempt((n) => n + 1)
  }
  return { draft, update, saving, saveError, retrySave }
}

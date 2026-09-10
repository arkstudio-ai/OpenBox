import { useEffect, useRef, useState } from "react"
import type { NextStepSuggestion, SuggestionsPart } from "@/shared/types/api"

interface Options {
  busy: boolean
  draft: string
  hasAttachments: boolean
  suggestions?: SuggestionsPart
  sessionKey?: string
  onSend: (prompt: string) => void | Promise<void>
  onFill: (prompt: string) => void
  onFailure: (prompt: string) => void
}

/** A persisted deadline survives refresh and bounds a wait after worker death. */
function useSuggestionWait(suggestions?: SuggestionsPart) {
  const key = suggestions?.status === "pending" && suggestions.expires_at
    ? `${suggestions.id}:${suggestions.expires_at}` : undefined
  const deadline = suggestions?.expires_at
  const [wait, setWait] = useState({ key, active: false })
  if (wait.key !== key) setWait({ key, active: false })
  useEffect(() => {
    if (!key || !deadline) return
    const remaining = Date.parse(deadline) - Date.now()
    if (!Number.isFinite(remaining) || remaining <= 0) return
    // Arm on the next tick so expired snapshots never flash a placeholder.
    const start = window.setTimeout(() => setWait({ key, active: true }), 0)
    const end = window.setTimeout(() => setWait({ key, active: false }), Math.min(remaining, 60_000))
    return () => { window.clearTimeout(start); window.clearTimeout(end) }
  }, [key, deadline])
  return key !== undefined && key === wait.key && wait.active
}

/** Local composer state is deliberately separate from persisted suggestions.
 *  A click sends through the normal chat path, never an auxiliary model path. */
export function useComposerSuggestions({
  busy, draft, hasAttachments, suggestions, sessionKey, onSend, onFill, onFailure,
}: Options) {
  const pending = useRef<{ cancelled: boolean } | null>(null)
  const [submittedId, setSubmittedId] = useState<string>()
  useEffect(() => () => {
    if (pending.current) pending.current.cancelled = true
    pending.current = null
  }, [sessionKey])

  const waiting = useSuggestionWait(suggestions)
  const eligible = !busy && !draft && !hasAttachments && suggestions?.id !== submittedId
  const loading = eligible && waiting
  const visible = eligible && suggestions?.status !== "pending" && suggestions?.status !== "unavailable"
    && suggestions?.items.length ? suggestions : undefined

  const select = (item: NextStepSuggestion) => {
    if (!visible || pending.current) return
    if (item.mode === "draft") {
      onFill(item.prompt)
      return
    }
    const request = { cancelled: false }
    pending.current = request
    setSubmittedId(visible.id)
    void Promise.resolve().then(() => {
      if (!request.cancelled) return onSend(item.prompt)
    }).catch(() => {
      // A late failure from another session must never overwrite this draft.
      if (!request.cancelled) onFailure(item.prompt)
    }).finally(() => {
      if (pending.current === request) pending.current = null
    })
  }
  return { visible, loading, select }
}

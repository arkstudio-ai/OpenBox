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

  const visible = !busy && !draft && !hasAttachments && suggestions?.id !== submittedId
    ? suggestions : undefined

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
  return { visible, select }
}

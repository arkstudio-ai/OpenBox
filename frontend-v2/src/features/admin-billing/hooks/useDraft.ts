import { useState } from "react"

/**
 * A local, editable copy of committed filter values.
 *
 * The filter bar has an apply button, so typing must not hit the server (each
 * read is also an audit row, §4.7). But the committed values live in the URL,
 * which the back button can change underneath us — so the draft re-syncs when
 * the committed value changes, using the documented "adjust state during
 * render" pattern rather than an effect that would paint the stale draft first.
 */
export function useDraft<T>(committed: T): [T, (next: T) => void] {
  const signature = JSON.stringify(committed)
  const [draft, setDraft] = useState(committed)
  const [seen, setSeen] = useState(signature)
  if (seen !== signature) {
    setSeen(signature)
    setDraft(committed)
  }
  return [draft, setDraft]
}

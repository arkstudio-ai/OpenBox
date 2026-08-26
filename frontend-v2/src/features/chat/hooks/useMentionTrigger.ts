// Opening the mention menu without a keystroke — the composer's "+" menu entry
// for the resource centre. It types the "@" that useMentionMenu keys off, so
// browsing and typing share one code path instead of two.
import { useCallback } from "react"
import type { RefObject } from "react"
import { insertMentionTrigger } from "../lib/trigger-insert"

interface Args {
  text: string
  textareaRef: RefObject<HTMLTextAreaElement | null>
  onReplace: (nextText: string, nextCaret: number) => void
}

export function useMentionTrigger({ text, textareaRef, onReplace }: Args) {
  return useCallback(() => {
    const ta = textareaRef.current
    const caret = ta ? ta.selectionStart : text.length
    const next = insertMentionTrigger(text, caret)
    onReplace(next.text, next.caret)
    // The caret must land after the "@" or the menu resolves an empty trigger.
    window.requestAnimationFrame(() => {
      ta?.focus()
      ta?.setSelectionRange(next.caret, next.caret)
    })
  }, [text, textareaRef, onReplace])
}

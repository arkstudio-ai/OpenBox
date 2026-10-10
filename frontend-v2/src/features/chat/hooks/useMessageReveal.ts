// Someone outside the chat — a voice call handing a turn to the assistant —
// asks to see a message. It usually reaches this list a moment after the ask,
// over the agent socket, so the row is looked for again briefly before giving
// up. Only rows already in this list scroll; nothing ever navigates.
import { useEffect, type RefObject } from "react"
import { onAppEvent } from "@/shared/events/bus"

const RETRY_MS = 150
const GIVE_UP_MS = 6_000

function reveal(row: HTMLElement, viewport: HTMLElement): void {
  const box = row.getBoundingClientRect()
  const view = viewport.getBoundingClientRect()
  // Already on screen: the reader stays where they are.
  if (box.top >= view.top && box.bottom <= view.bottom) return
  // Centred rather than at an edge, where the call window floats over the list.
  row.scrollIntoView?.({ block: "center", behavior: "smooth" })
}

export function useMessageReveal(scrollRef: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    let timer: number | undefined
    const stop = onAppEvent("chat.reveal", ({ messageId }) => {
      window.clearTimeout(timer)
      const deadline = Date.now() + GIVE_UP_MS
      const attempt = () => {
        const viewport = scrollRef.current
        const rows = viewport?.querySelectorAll<HTMLElement>("[data-turn-key]") ?? []
        const row = Array.from(rows).find((element) => element.dataset.turnKey === messageId)
        if (row && viewport) reveal(row, viewport)
        else if (Date.now() < deadline) timer = window.setTimeout(attempt, RETRY_MS)
      }
      attempt()
    })
    return () => {
      stop()
      window.clearTimeout(timer)
    }
  }, [scrollRef])
}

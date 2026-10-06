import { createContext } from "react"
import type { AssistantSnapshot } from "../api/assistant"

/** Present only on the main assistant page: the unread-answer snapshot and the
 *  read-cursor receipt for a final answer that was actually seen. */
export const AssistantReadContext = createContext<{
  snapshot: AssistantSnapshot
  displayed: (messageId: string) => void
} | null>(null)

import { createContext } from "react"
import type { AssistantSnapshot } from "../api/assistant"

export const AssistantReadContext = createContext<{
  snapshot: AssistantSnapshot
  displayed: (messageId: string) => void
} | null>(null)


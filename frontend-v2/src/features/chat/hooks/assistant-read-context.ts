import { createContext } from "react"
import type { AssistantSnapshot } from "../api/assistant"
import type { MessageWithParts } from "@/shared/types/api"

export const AssistantReadContext = createContext<{
  snapshot?: AssistantSnapshot
  displayed?: (messageId: string) => void
  sourcesPending?: boolean
  sourcesAvailable?: boolean
  pendingIds?: ReadonlySet<string>
  unavailableIds?: ReadonlySet<string>
  transcript?: ReadonlyMap<string, MessageWithParts>
} | null>(null)

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
  /** Same-scope snapshot denials survive omission; null means no comparable server clock. */
  sourceDenials?: ReadonlyMap<string, string | null>
} | null>(null)

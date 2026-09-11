import type { SessionStatus, SuggestionsPart } from "@/shared/types/api"
import type { Turn } from "./turn-view"

interface Visibility {
  readOnly?: boolean
  hasError?: boolean
  permissionCount?: number
  questionCount?: number
}

/** Only the current answer owns the composer chips; never fall back to an
 *  older turn if the current one is running, failed, aborted, or has no chips. */
export function latestSuggestions(
  turns: Turn[],
  status: SessionStatus | undefined,
  { readOnly = false, hasError = false, permissionCount = 0, questionCount = 0 }: Visibility = {},
): SuggestionsPart | undefined {
  // The dock stays pinned while reading history. Toggling it on scroll changes
  // the viewport height and can repeatedly pull a near-bottom scroll back down.
  if (readOnly || hasError || permissionCount > 0 || questionCount > 0 || status !== "idle") return undefined
  const turn = turns[turns.length - 1]
  if (turn?.kind !== "assistant" || turn.meta.finish !== "stop" || turn.meta.error) return undefined
  const message = turn.messages[turn.messages.length - 1]
  if (message.summary) return undefined
  return message.parts.find((part): part is SuggestionsPart => part.type === "suggestions")
}

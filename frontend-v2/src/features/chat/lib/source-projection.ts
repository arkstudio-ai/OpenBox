import type { ContextType } from "react"
import type { MessageWithParts } from "@/shared/types/api"
import type { AssistantReadContext } from "../hooks/assistant-read-context"

/** Choose a whole authorized projection. Never merge removed parts back in. */
export function sourceProjection(message: MessageWithParts, context: ContextType<typeof AssistantReadContext>): MessageWithParts {
  if (!context) return message
  const checked = context.transcript?.get(message.id)
  const current = checked && (checked.source_checked_at ?? "") >= (message.source_checked_at ?? "") ? checked : message
  if (context.sourcesAvailable === false || context.unavailableIds?.has(message.id) ||
    context.snapshot?.answers.some((answer) => answer.message_id === message.id && !answer.available)) {
    return { ...current, parts: [], source_status: "unavailable" }
  }
  if (!current.id.startsWith("tmp-") && (context.sourcesPending || context.pendingIds?.has(message.id) || !checked || !current.source_status)) return { ...current, parts: [], source_status: "pending" }
  return current
}

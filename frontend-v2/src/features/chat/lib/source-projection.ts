import type { ContextType } from "react"
import type { MessageWithParts } from "@/shared/types/api"
import type { AssistantReadContext } from "../hooks/assistant-read-context"
import type { AssistantSnapshot } from "../api/assistant"

type ReadContext = ContextType<typeof AssistantReadContext>

/** Canonical UTC ordering preserves the database's microseconds, unlike Date.parse alone. */
function sourceTime(value?: string | null): string | null {
  if (!value) return null
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(value)
  if (!match || !Number.isFinite(Date.parse(value))) return null
  return `${match[1]}.${(match[2] ?? "").padEnd(6, "0")}`
}

/** Call at commit, never while rendering. Keep the floor even after a later read succeeds. */
export function rememberSnapshotDenials(denials: Map<string, string | null>, snapshot: AssistantSnapshot) {
  for (const answer of snapshot.answers) {
    if (answer.available) continue
    const next = sourceTime(snapshot.source_checked_at)
    const previous = denials.get(answer.message_id)
    if (previous === undefined || (previous !== null && (next === null || next > previous))) {
      denials.set(answer.message_id, next)
    }
  }
}

function denialFloor(messageId: string, context: ReadContext): string | null | undefined {
  const held = context?.sourceDenials?.get(messageId)
  if (!context?.snapshot?.answers.some((answer) => answer.message_id === messageId && !answer.available)) return held
  const current = sourceTime(context.snapshot.source_checked_at)
  if (held === null || current === null) return null
  return held === undefined || current > held ? current : held
}

/** Only a current-source API projection can supersede a known deny, never raw message props. */
export function sourceDenied(messageId: string, context: ReadContext, proof = context?.transcript?.get(messageId)): boolean {
  const floor = denialFloor(messageId, context)
  if (floor === undefined) return false
  const checkedAt = sourceTime(proof?.source_checked_at)
  return floor === null || proof?.id !== messageId || proof.source_status !== "available" || checkedAt === null || checkedAt <= floor
}

/** Choose a whole authorized projection. Never merge removed parts back in. */
export function sourceProjection(message: MessageWithParts, context: ReadContext): MessageWithParts {
  if (!context) return message
  const checked = context.transcript?.get(message.id)
  const knownDenial = denialFloor(message.id, context) !== undefined
  const current = checked && (knownDenial || (checked.source_checked_at ?? "") >= (message.source_checked_at ?? "")) ? checked : message
  if (context.sourcesAvailable === false || context.unavailableIds?.has(message.id) || sourceDenied(message.id, context)) {
    return { ...current, parts: [], source_status: "unavailable" }
  }
  if (!current.id.startsWith("tmp-") && (context.sourcesPending || context.pendingIds?.has(message.id) || !checked || !current.source_status)) return { ...current, parts: [], source_status: "pending" }
  return current
}

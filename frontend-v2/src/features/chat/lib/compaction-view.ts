import type { CompactionPart, MessageWithParts } from "@/shared/types/api"

export interface CompactionView {
  id: string
  status: "running" | "completed" | "failed" | "interrupted"
  summary: string
}

export function compactionPart(message: MessageWithParts): CompactionPart | undefined {
  return message.parts.find((part): part is CompactionPart => part.type === "compaction")
}

export function isCompactionRequest(message: MessageWithParts): boolean {
  return message.role === "user" && (message.agent === "compaction" || Boolean(compactionPart(message)))
}

export function isCompactionMessage(message: MessageWithParts): boolean {
  return isCompactionRequest(message) || (message.role === "assistant" &&
    (message.agent === "compaction" || message.summary === true))
}

/** One process item per request, stable across deltas, settlement and paging.
 *  `agent` identifies the summary before its first token; `summary` alone is
 *  too late because the backend sets it only when the attempt settles. */
export function buildCompactionViews(messages: MessageWithParts[], streaming: boolean): CompactionView[] {
  const entries = new Map<string, { request?: MessageWithParts; summary?: MessageWithParts }>()
  for (const message of messages) {
    if (!isCompactionMessage(message)) continue
    const request = isCompactionRequest(message)
    const id = request ? message.id : (message.parent_id ?? message.id)
    entries.set(id, { ...entries.get(id), [request ? "request" : "summary"]: message })
  }
  return [...entries].map(([id, entry]) => {
    const descriptor = entry.request ? compactionPart(entry.request) : undefined
    const attempt = entry.summary
    const text = attempt?.parts
      .filter((part) => part.type === "text")
      .map((part) => part.text)
      .join("") || descriptor?.summary || ""
    const failed = Boolean(attempt?.error) || attempt?.finish === "error"
    const completed = !failed && (attempt?.finish === "stop" ||
      Boolean(descriptor?.replacement_id) || Boolean(descriptor?.summary))
    const last = attempt ?? entry.request
    const live = streaming && last?.id === messages.at(-1)?.id && !attempt?.finish
    return {
      id,
      summary: text,
      status: failed ? "failed" : completed ? "completed" : live ? "running" : "interrupted",
    }
  })
}

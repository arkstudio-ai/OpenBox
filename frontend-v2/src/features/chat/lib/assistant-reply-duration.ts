import type { MessageWithParts } from "@/shared/types/api"

/** Never estimate a main-assistant reply from provider steps or read time. */
export function assistantReplyDuration(message: MessageWithParts | undefined): number {
  if (message?.finish !== "stop") return 0
  const timing = message.assistant_timing
  if (typeof timing?.accepted_at !== "string" || typeof timing?.settled_at !== "string") return 0
  const start = Date.parse(timing.accepted_at)
  const end = Date.parse(timing.settled_at)
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? (end - start) / 1000 : 0
}

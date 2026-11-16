import type { RecordedVoice } from "../api/recorded-voice"
import type { MessageWithParts } from "@/shared/types/api"

/** Stable client id for optimistic echo reconciliation (see stream store). */
export function makeClientId(): string {
  return `cmid-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/** Build the optimistic user message shown the instant a prompt is sent. */
export function optimisticUserMessage(
  sessionId: string,
  text: string,
  clientMessageId: string,
  voice?: RecordedVoice,
): MessageWithParts {
  return {
    id: `tmp-${clientMessageId}`,
    session_id: sessionId,
    role: "user",
    parts: [{ type: "text", id: `tmp-part-${clientMessageId}`, text, ...(voice ? { origin_ref: { voice_message: { asset_id: voice.asset_id, duration_ms: voice.duration_ms } } } : {}) }],
    created_at: new Date().toISOString(),
    client_message_id: clientMessageId,
  }
}

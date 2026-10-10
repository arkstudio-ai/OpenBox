import type { MessageWithParts, TextPart } from "@/shared/types/api"

/** The server's contextual handover, displayed separately from the human transcript. */
export function voiceRequest(message: MessageWithParts): string | null {
  if (message.role !== "user") return null
  const parts = message.parts.filter((part): part is TextPart => part.type === "text" && !part.synthetic)
  if (parts.length !== 1) return null
  const part = parts[0]
  if (part.origin !== "human" || part.origin_ref?.entrypoint !== "assistant_voice") return null
  const context = part.origin_ref.voice_context
  if (!context || typeof context !== "object" || Array.isArray(context)) return null
  const request = (context as Record<string, unknown>).request
  if (typeof request !== "string" || !request.trim()) return null
  return request.trim().replace(/\s+/g, " ") === part.text.trim().replace(/\s+/g, " ") ? null : request.trim()
}

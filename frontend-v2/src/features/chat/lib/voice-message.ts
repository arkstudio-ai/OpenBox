import type { MessageWithParts } from "@/shared/types/api"

export function recordedVoice(message: MessageWithParts): { assetId: string; durationMs: number } | null {
  for (const part of message.parts) {
    if (part.type !== "text") continue
    const value = part.origin_ref?.voice_message
    if (!value || typeof value !== "object") continue
    const data = value as Record<string, unknown>
    if (typeof data.asset_id === "string" && typeof data.duration_ms === "number") {
      return { assetId: data.asset_id, durationMs: data.duration_ms }
    }
  }
  return null
}

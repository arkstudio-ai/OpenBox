import {
  isMediaEnvelope,
  isPayloadEnvelope,
  type MediaEnvelope,
  type PayloadEnvelope,
} from "../../types/protocol"
import { isPlainObject } from "../../utils/python"

export type MediaKind = "image" | "audio" | "video"

export function mediaKindOf(mediaType: string): MediaKind | null {
  if (mediaType.startsWith("image/")) return "image"
  if (mediaType.startsWith("audio/")) return "audio"
  if (mediaType.startsWith("video/")) return "video"
  return null
}

export type ContentEnvelope = PayloadEnvelope | MediaEnvelope

export function isContentEnvelope(value: unknown): value is ContentEnvelope {
  return isPayloadEnvelope(value) || isMediaEnvelope(value)
}

/** Every retained-content wrapper inside a captured value, depth-first, bounded. */
export function findEnvelopes(value: unknown, limit = 20, depth = 0): ContentEnvelope[] {
  if (isContentEnvelope(value)) return [value]
  if (depth > 6) return []
  const children = Array.isArray(value) ? value : isPlainObject(value) ? Object.values(value) : []
  const found: ContentEnvelope[] = []
  for (const child of children) {
    if (found.length >= limit) break
    found.push(...findEnvelopes(child, limit - found.length, depth + 1))
  }
  return found
}

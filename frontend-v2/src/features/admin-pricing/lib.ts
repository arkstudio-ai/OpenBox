// Display rules shared by the table and the editor: which fields a kind has,
// how a margin reads, which tone a flag takes.
import type { StatusTone } from "@/shared/ui/StatusPill"
import type { PriceFragment, PricingFlag, PricingKind } from "./types"

export const DASH = "—"

/** The editable price fields per kind, in display order. */
export const FIELDS: Record<PricingKind, readonly string[]> = {
  llm: ["input", "output", "cache_read", "cache_write"],
  "video-gen": ["per_second"],
  "image-gen": ["per_image"],
  stt: ["per_minute"],
  "voice-realtime": ["input_text", "input_audio", "output_text", "output_audio"],
  "ims-compose": ["per_minute"],
  "hot-trends": ["per_fetch"],
}

/** Fields the server insists on for a sale fragment of this kind. */
export const REQUIRED: Record<PricingKind, readonly string[]> = {
  llm: ["input", "output"],
  "video-gen": ["per_second"],
  "image-gen": ["per_image"],
  stt: ["per_minute"],
  "voice-realtime": ["input_text", "input_audio", "output_text", "output_audio"],
  "ims-compose": ["per_minute"],
  "hot-trends": ["per_fetch"],
}

/** A sample usage the preview prices, per kind. */
export const SAMPLE_USAGE: Record<PricingKind, Record<string, number>> = {
  llm: { input: 1_000_000, output: 100_000 },
  "video-gen": { seconds: 10 },
  "image-gen": { images: 1 },
  stt: { seconds: 60 },
  "voice-realtime": { input_text: 2_000, input_audio: 60_000, output_text: 2_000, output_audio: 60_000 },
  "ims-compose": { seconds: 60 },
  "hot-trends": {},
}

/** Flat field → value view of a fragment (voice prices nest under per_million). */
export function fieldValues(kind: PricingKind, fragment: PriceFragment | null | undefined): Record<string, string> {
  if (!fragment) return {}
  const out: Record<string, string> = {}
  for (const field of FIELDS[kind]) {
    const value = kind === "voice-realtime" ? fragment.per_million?.[field] : fragment[field]
    if (typeof value === "string" && value !== "") out[field] = value
  }
  return out
}

/** The inverse: a fragment the server accepts, from edited field strings. */
export function toFragment(kind: PricingKind, values: Record<string, string>): PriceFragment {
  const entries = Object.entries(values).filter(([, v]) => v.trim() !== "")
  if (kind === "voice-realtime") {
    return { per_million: Object.fromEntries(entries.map(([k, v]) => [k, v.trim()])) }
  }
  return Object.fromEntries(entries.map(([k, v]) => [k, v.trim()]))
}

const DECIMAL = /^(\d+)(\.\d{1,12})?$/

export function isDecimal(value: string): boolean {
  return DECIMAL.test(value.trim())
}

export function fragmentValid(kind: PricingKind, values: Record<string, string>): boolean {
  const filled = Object.entries(values).filter(([, v]) => v.trim() !== "")
  if (filled.some(([, v]) => !isDecimal(v))) return false
  return REQUIRED[kind].every((field) => (values[field] ?? "").trim() !== "")
}

/** "+455.6%" / "−18.0%" / "∞" / "—". */
export function formatMargin(pct: string | null | undefined): string {
  if (pct == null) return DASH
  if (pct === "inf") return "∞"
  const n = Number(pct)
  if (!Number.isFinite(n)) return DASH
  const sign = n > 0 ? "+" : n < 0 ? "−" : ""
  return `${sign}${Math.abs(n).toFixed(1)}%`
}

export function marginTone(pct: string | null | undefined): "danger" | "warn" | "ok" | "muted" {
  if (pct == null) return "muted"
  if (pct === "inf") return "ok"
  const n = Number(pct)
  if (!Number.isFinite(n)) return "muted"
  if (n < 0) return "danger"
  if (n < 10) return "warn"
  return "ok"
}

export const FLAG_TONES: Record<PricingFlag, StatusTone> = {
  disabled: "danger",
  unpriced: "danger",
  below_cost: "danger",
  expiring: "warn",
  no_cost: "warn",
  alias: "muted",
  overridden: "accent",
  unused_30d: "muted",
  configured: "muted",
}

/** Flags worth a pill in the table; `configured` and `unused_30d` are filters, not badges. */
export const BADGE_FLAGS: readonly PricingFlag[] = ["disabled", "unpriced", "below_cost", "expiring", "no_cost", "alias", "overridden"]

export const FILTER_FLAGS = ["below_cost", "unpriced", "expiring", "overridden", "no_cost", "configured", "unused_30d"] as const

export function newRequestKey(): string {
  return crypto.randomUUID().replace(/-/g, "")
}

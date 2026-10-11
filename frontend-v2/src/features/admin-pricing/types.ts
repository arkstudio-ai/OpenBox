// Shapes of /api/admin/pricing. Prices are decimal strings end to end; the
// page formats them and never does arithmetic on them (margins arrive computed).

export type PricingKind =
  | "llm"
  | "video-gen"
  | "image-gen"
  | "stt"
  | "voice-realtime"
  | "ims-compose"
  | "hot-trends"

export const PRICING_KINDS: readonly PricingKind[] = [
  "llm",
  "video-gen",
  "image-gen",
  "stt",
  "voice-realtime",
  "ims-compose",
  "hot-trends",
]

export type PricingFlag =
  | "disabled"
  | "unpriced"
  | "below_cost"
  | "no_cost"
  | "expiring"
  | "alias"
  | "overridden"
  | "unused_30d"
  | "configured"

/** Price fields by kind. LLM and voice carry several; the rest one unit price. */
export type PriceFragment = Record<string, unknown> & {
  input?: string
  output?: string
  cache_read?: string
  cache_write?: string
  per_second?: string
  per_image?: string
  per_minute?: string
  min_minutes?: number
  per_fetch?: string
  per_million?: Record<string, string>
  currency?: string
  basis?: string
  channel?: string
  source?: string
  verified_at?: string
}

export interface PricingRule {
  id: string
  key: string
  revision: number
  status: "active" | "disabled"
  sale: PriceFragment | null
  cost: PriceFragment | null
  valid_from: string | null
  valid_until: string | null
  reason: string
  actor_user_id: string
  audit_id: string | null
  created_at: string
  superseded_at: string | null
}

export interface PricingUsage {
  events: number
  charged_events: number
  credits: string
  shadow_credits: string
  cost_credits: string
  costed_events: number
  tokens: number
  unpriced_events: number
  gross_margin: string | null
}

export interface PricingItem {
  key: string
  kind: PricingKind
  model: string
  resolution: string | null
  label: string
  unit: string
  alias_of: string | null
  channel: string | null
  base_sale: PriceFragment | null
  sale: PriceFragment | null
  cost: PriceFragment | null
  /** Sale prices in credits per unit, by field (llm: input/output/…; others: the unit). */
  sale_credits: Record<string, string>
  cost_credits: Record<string, string>
  /** Percent strings per field; null when no cost; "inf" when cost is zero. */
  margin_pct: Record<string, string | null>
  below_cost_fields: string[]
  flags: PricingFlag[]
  expires_at: string | null
  rule: PricingRule | null
  usage_30d: PricingUsage | null
}

export interface PricingTable {
  as_of: string
  window_days: number
  catalogue_version: string
  base_version: string
  usd_cny: string
  rules_loaded_at: string | null
  effective_within_seconds: number
  summary: {
    credits: string
    cost_credits: string
    gross_margin: string
    costed_events: number
    charged_events: number
    flags: Record<string, number>
  }
  items: PricingItem[]
}

export interface PricingHistory {
  key: string
  revisions: PricingRule[]
  operations: Array<{
    id: string
    action: string
    actor: { id: string; username?: string }
    created_at: string
    reason: string
    before: PricingRule | null
    result: Record<string, unknown> | null
  }>
}

export interface PricingWriteBody {
  request_key: string
  reason: string
  expected_revision: number
  status?: "active" | "disabled"
  sale?: PriceFragment | null
  cost?: PriceFragment | null
  valid_until?: string | null
  allow_below_cost?: boolean
  confirm_disable?: boolean
}

export interface PricingWriteResult {
  key: string
  rule: PricingRule | null
  below_cost_fields?: string[]
  reverted?: PricingRule
  effective_within_seconds: number
  replayed?: boolean
}

export interface PricingPreviewResult {
  key: string
  usage: Record<string, number>
  current: { credits: string | null; cost_credits: string | null; reason: string | null }
  draft: { credits: string | null; cost_credits: string | null; reason: string | null } | null
}

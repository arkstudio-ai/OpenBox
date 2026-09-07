export interface FleetDesktop {
  id: string
  desktop_id?: string | null
  ecd_end_user_ids?: string[] | null
  ecd_end_users?: Array<{ id: string; username?: string | null }> | null
  workspace_id?: string | null
  pool_state: string
  status: string
  tunnel_state: string
  charge_type?: string | null
  expires_at?: string | null
  spec?: string | null
  golden_image_id?: string | null
}

export interface FleetAlert {
  id: string
  rule: string
  severity: "info" | "warn" | "critical"
  resource_id: string
  message: string
  first_seen_at: string
  last_seen_at: string
  acked_at?: string | null
  muted_until?: string | null
}

export interface PoolSummary {
  states: Record<string, number>
  target_prewarm: number
  purchased_today: number
  enabled: boolean
  auto_purchase: boolean
  auto_renew: boolean
  gates: {
    max_unit_price_cny: number
    max_per_tick: number
    max_per_day: number
    min_balance_multiple: number
  }
}

export interface PoolEnsureResult {
  status: "disabled" | "satisfied" | "blocked" | "dry_run" | "purchased"
  current: number
  target: number
  gap: number
  quantity: number
  unit_price?: number
  currency?: string
  gate?: string
  message?: string
  created?: string[]
}

export interface FleetSnapshot {
  taken_at?: string | null
  sources: Array<{ source: string; ok: boolean; error?: string | null }>
}

/** One row of the desktop timeline (`desktop_events`). */
export interface DesktopEvent {
  id: string
  ts: string | null
  desktop_id?: string | null
  container_key?: string | null
  session_id?: string | null
  tool_call_id?: string | null
  request_id?: string | null
  kind: string
  status: "ok" | "fail" | "timeout" | "info" | string
  duration_ms?: number | null
  summary: string
  diag_id?: string | null
  detail?: Record<string, unknown> | null
}

export type DiagLight = "ok" | "degraded" | "down" | "unknown"

export interface DiagSummary {
  lights: Record<string, DiagLight>
  findings: string[]
}

/** A `browser.diag` row, flattened: the event plus what the collector saw. */
export interface DiagRecord extends DesktopEvent {
  reason?: string
  error?: string
  note?: string
  collected?: boolean
  via?: string | null
  lights?: Record<string, DiagLight> | null
  findings?: string[] | null
  report?: DiagReport | null
}

interface LogTail {
  path?: string
  lines?: string[]
  missing?: boolean
  error?: string
}

/** The collector's report; only the parts the drawer renders are typed. */
export interface DiagReport {
  diag_version: string
  collected_at: string
  elapsed_ms?: number
  via?: string
  errors: Array<{ section: string; error: string }>
  summary: DiagSummary | null
  chrome?: { log?: LogTail; total_processes?: number; total_threads?: number } | null
  relay?: { log?: LogTail } | null
  logs?: { journal?: { lines?: string[]; error?: string; skipped?: boolean } } | null
  [section: string]: unknown
}

export interface DiagCollectResult extends DiagReport {
  id: string
  fallback_errors: string[]
}

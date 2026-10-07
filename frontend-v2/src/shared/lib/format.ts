// All date/number formatting goes through here with the active locale
// (ENGINEERING_SPEC §10.6) — never bare toLocaleString().
import i18n from "@/shared/i18n"

function locale(): string {
  return i18n.language || "zh-CN"
}

export function formatDateTime(iso: string): string {
  return new Intl.DateTimeFormat(locale(), { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso))
}

export function formatRelative(iso: string): string {
  const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: "auto" })
  const diff = (new Date(iso).getTime() - Date.now()) / 1000
  const abs = Math.abs(diff)
  if (abs < 60) return rtf.format(Math.round(diff), "second")
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute")
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour")
  return rtf.format(Math.round(diff / 86400), "day")
}

/** "3 hours ago" within the last week, then a short calendar date — with the
 *  year only once it differs from this one. For lists people scan, not audits. */
export function formatSince(iso: string): string {
  const date = new Date(iso)
  if (Math.abs(Date.now() - date.getTime()) < 7 * 86_400_000) return formatRelative(iso)
  const year = date.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" as const }
  return new Intl.DateTimeFormat(locale(), { month: "short", day: "numeric", ...year }).format(date)
}

export function formatNumber(n: number): string {
  return new Intl.NumberFormat(locale()).format(n)
}

export function formatTokens(n: number): string {
  const round = (v: number) => new Intl.NumberFormat(locale(), { maximumFractionDigits: 1 }).format(v)
  // Context windows reach seven figures, and "1,000k" is a worse way to write
  // "1M" — the thousands separator makes it read as a thousand of something.
  if (n >= 1_000_000) return `${round(n / 1_000_000)}M`
  if (n >= 1000) return `${round(n / 1000)}k`
  return String(n)
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B"
  const k = 1024
  const sizes = ["B", "KB", "MB", "GB"]
  const i = Math.min(sizes.length - 1, Math.floor(Math.log(bytes) / Math.log(k)))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`
}

export function formatDuration(seconds: number): string {
  if (seconds < 10) return `${seconds.toFixed(1)}s`
  if (seconds < 60) return `${Math.round(seconds)}s`
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
}

/** A running clock, "02:14"; the minutes keep counting past the hour. */
export function formatClock(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`
}

function finite(value: string | number): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

/** Exactly `digits` decimals, for copy that carries its own currency sign ("约 ¥{{yuan}}"). */
export function formatAmount(value: string | number, digits: number): string {
  return new Intl.NumberFormat(locale(), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(finite(value))
}

/** Yuan with its sign, "¥0.0035". Per-call estimates live in fractions of a yuan,
 *  so the decimals are fixed rather than trimmed. */
export function formatYuan(value: string | number, digits = 4): string {
  return new Intl.NumberFormat(locale(), {
    style: "currency",
    currency: "CNY",
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(finite(value))
}

export function formatCost(usd: number): string {
  return new Intl.NumberFormat(locale(), {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 3,
  }).format(usd)
}

/** Presentation only; settlement uses server-side Decimal and returns strings. */
export function formatCredits(value: string | number | null | undefined): string {
  if (value == null) return "—"
  const n = Number(value)
  if (!Number.isFinite(n)) return "—"
  return new Intl.NumberFormat(locale(), {
    maximumFractionDigits: Math.abs(n) < 0.001 ? 12 : 6,
  }).format(n)
}

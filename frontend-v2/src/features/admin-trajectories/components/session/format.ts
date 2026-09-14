// Compact clock text for dense rows: time of day with milliseconds in the
// chosen zone, or Unix seconds. Full dates with zone names live in the
// inspector (utils/time formatInstant).
import type { TimeMode } from "../../stores/view"
import { epochMs } from "../../utils/time"

const formatters = new Map<string, Intl.DateTimeFormat>()

function formatter(locale: string, utc: boolean): Intl.DateTimeFormat {
  const key = `${locale}|${utc}`
  let cached = formatters.get(key)
  if (!cached) {
    cached = new Intl.DateTimeFormat(locale, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      fractionalSecondDigits: 3,
      hourCycle: "h23",
      ...(utc ? { timeZone: "UTC" } : {}),
    })
    formatters.set(key, cached)
  }
  return cached
}

export function formatClock(iso: string | null | undefined, mode: TimeMode, locale: string): string | null {
  const millis = epochMs(iso)
  if (millis === null) return null
  if (mode === "unix") return (Math.floor(millis) / 1000).toFixed(3)
  return formatter(locale, mode === "utc").format(new Date(Math.floor(millis)))
}

/** First eight characters of an opaque id, for labels that also offer the full value. */
export function shortId(id: string | null | undefined): string {
  if (!id) return ""
  return id.length > 12 ? `${id.slice(0, 8)}…` : id
}

// Time display for the inspector. Instants show milliseconds with an explicit
// zone (local or UTC) or exact Unix seconds; durations use Intl units. A clock
// for "still running" is the replay position's recorded time during replay,
// so a paused historical view never keeps ticking with the wall clock.
import type { TimeMode } from "../stores/view"
import { isoMicros } from "./python"

export function formatInstant(iso: string | null | undefined, mode: TimeMode, locale: string): string | null {
  if (!iso) return null
  const micros = isoMicros(iso)
  if (micros === null) return iso
  const millis = Math.floor(micros / 1000)
  if (mode === "unix") {
    const seconds = Math.floor(millis / 1000)
    const fraction = String(((millis % 1000) + 1000) % 1000).padStart(3, "0")
    return `${seconds}.${fraction}`
  }
  const formatter = new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
    hourCycle: "h23",
    timeZoneName: "short",
    ...(mode === "utc" ? { timeZone: "UTC" } : {}),
  })
  return formatter.format(new Date(millis))
}

/** Localised duration; null in, null out, so a caller can say "not recorded". */
export function formatDuration(ms: number | null | undefined, locale: string): string | null {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return null
  if (ms < 1000) {
    return new Intl.NumberFormat(locale, {
      style: "unit",
      unit: "millisecond",
      unitDisplay: "short",
      maximumFractionDigits: 1,
    }).format(ms)
  }
  if (ms < 60_000) {
    return new Intl.NumberFormat(locale, {
      style: "unit",
      unit: "second",
      unitDisplay: "short",
      maximumFractionDigits: 2,
    }).format(ms / 1000)
  }
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  const unit = (value: number, name: "minute" | "second") =>
    new Intl.NumberFormat(locale, { style: "unit", unit: name, unitDisplay: "narrow" }).format(value)
  return `${unit(minutes, "minute")} ${unit(seconds, "second")}`
}

/** Exact millisecond count for tooltips: "1,814 ms" rather than "1.81 s". */
export function formatExactMs(ms: number | null | undefined, locale: string): string | null {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return null
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit: "millisecond",
    unitDisplay: "short",
    maximumFractionDigits: 3,
  }).format(ms)
}

/**
 * Elapsed time of an open interval measured against `clockIso`: the playhead's
 * recorded time in replay, the current time when following live. Estimates are
 * labelled as such by the caller; they are never stored as a duration.
 */
export function elapsedUntil(startIso: string | null, clockIso: string | null): number | null {
  if (!startIso || !clockIso) return null
  const start = isoMicros(startIso)
  const clock = isoMicros(clockIso)
  if (start === null || clock === null) return null
  return Math.max(0, (clock - start) / 1000)
}

export function epochMs(iso: string | null | undefined): number | null {
  if (!iso) return null
  const micros = isoMicros(iso)
  return micros === null ? null : micros / 1000
}

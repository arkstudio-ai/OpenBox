// Pure schedule helpers: build wire schedules from the form model, parse them
// back for editing, and describe them for display. No React, no i18n instance
// — callers pass `t`, so these stay unit-testable.
import type { CronSchedule } from "@/features/cron/types"
import {
  MIN_INTERVAL_MINUTES,
  SILENT_SENTINEL,
  WEEKDAY_KEYS,
  type IntervalUnit,
  type ScheduleMode,
} from "@/features/cron/constants"

export interface ScheduleForm {
  mode: ScheduleMode
  /** "HH:mm" for daily/weekly */
  time: string
  /** 0-6, Sunday=0, for weekly */
  weekday: number
  /** interval value + unit */
  every: number
  unit: IntervalUnit
  /** raw cron expression for custom */
  expr: string
}

export type Translate = (key: string, options?: Record<string, unknown>) => string

export const DEFAULT_FORM: ScheduleForm = {
  mode: "daily",
  time: "09:00",
  weekday: 1,
  every: 30,
  unit: "minutes",
  expr: "0 9 * * *",
}

export function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
}

function parseTime(time: string): { h: number; m: number } | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(time)
  if (!match) return null
  const h = Number(match[1])
  const m = Number(match[2])
  if (h < 0 || h > 23 || m < 0 || m > 59) return null
  return { h, m }
}

export function intervalMs(every: number, unit: IntervalUnit): number {
  return unit === "hours" ? every * 3_600_000 : every * 60_000
}

/** null means the form is invalid (empty expr, bad time, sub-minimum interval). */
export function buildSchedule(form: ScheduleForm, tz: string): CronSchedule | null {
  if (form.mode === "interval") {
    if (!Number.isFinite(form.every) || form.every <= 0) return null
    if (intervalMs(form.every, form.unit) < MIN_INTERVAL_MINUTES * 60_000) return null
    return { kind: "every", every_ms: intervalMs(form.every, form.unit) }
  }
  if (form.mode === "custom") {
    const expr = form.expr.trim()
    if (expr.split(/\s+/).length < 5) return null
    return { kind: "cron", expr, tz }
  }
  const time = parseTime(form.time)
  if (!time) return null
  if (form.mode === "weekly") {
    return { kind: "cron", expr: `${time.m} ${time.h} * * ${form.weekday}`, tz }
  }
  return { kind: "cron", expr: `${time.m} ${time.h} * * *`, tz }
}

const DAILY_RE = /^(\d{1,2}) (\d{1,2}) \* \* \*$/
const WEEKLY_RE = /^(\d{1,2}) (\d{1,2}) \* \* ([0-6])$/

const pad = (n: number) => String(n).padStart(2, "0")

/** Reconstruct the form model from a stored schedule (for the edit dialog). */
export function scheduleToForm(schedule: CronSchedule): ScheduleForm {
  if (schedule.kind === "every") {
    const ms = schedule.every_ms
    if (ms % 3_600_000 === 0) {
      return { ...DEFAULT_FORM, mode: "interval", every: ms / 3_600_000, unit: "hours" }
    }
    return { ...DEFAULT_FORM, mode: "interval", every: Math.round(ms / 60_000), unit: "minutes" }
  }
  if (schedule.kind === "cron") {
    const daily = DAILY_RE.exec(schedule.expr)
    if (daily) {
      return { ...DEFAULT_FORM, mode: "daily", time: `${pad(Number(daily[2]))}:${pad(Number(daily[1]))}` }
    }
    const weekly = WEEKLY_RE.exec(schedule.expr)
    if (weekly) {
      return {
        ...DEFAULT_FORM,
        mode: "weekly",
        time: `${pad(Number(weekly[2]))}:${pad(Number(weekly[1]))}`,
        weekday: Number(weekly[3]),
      }
    }
    return { ...DEFAULT_FORM, mode: "custom", expr: schedule.expr }
  }
  return { ...DEFAULT_FORM }
}

/** Human-readable schedule line, localized through the caller's `t`. */
export function describeSchedule(schedule: CronSchedule, t: Translate): string {
  if (schedule.kind === "at") {
    return t("cron:describe.once", { time: schedule.at })
  }
  if (schedule.kind === "every") {
    const ms = schedule.every_ms
    if (ms % 3_600_000 === 0) return t("cron:describe.everyHours", { count: ms / 3_600_000 })
    return t("cron:describe.everyMinutes", { count: Math.round(ms / 60_000) })
  }
  const daily = DAILY_RE.exec(schedule.expr)
  if (daily) {
    return t("cron:describe.daily", { time: `${pad(Number(daily[2]))}:${pad(Number(daily[1]))}` })
  }
  const weekly = WEEKLY_RE.exec(schedule.expr)
  if (weekly) {
    return t("cron:describe.weekly", {
      weekday: t(WEEKDAY_KEYS[Number(weekly[3])]),
      time: `${pad(Number(weekly[2]))}:${pad(Number(weekly[1]))}`,
    })
  }
  return t("cron:describe.cron", { expr: schedule.expr })
}

/** A run whose result is the sentinel (or empty) was deliberately silent. */
export function isSilentResult(summary: string | null): boolean {
  if (summary === null) return true
  const stripped = summary.trim()
  if (!stripped) return true
  const head = stripped.split("\n", 1)[0].trim().replace(/^[*_`]+|[*_`.。!!\s]+$/g, "")
  return head === SILENT_SENTINEL && stripped.length <= SILENT_SENTINEL.length + 8
}

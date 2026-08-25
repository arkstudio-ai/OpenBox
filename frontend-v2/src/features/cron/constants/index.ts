// Explicit value → i18n-key maps (ENGINEERING_SPEC §10.3: no dynamic key
// concatenation without a registered mapping table).

export const SCHEDULE_MODES = ["daily", "weekly", "interval", "custom"] as const
export type ScheduleMode = (typeof SCHEDULE_MODES)[number]

export const SCHEDULE_MODE_KEYS: Record<ScheduleMode, string> = {
  daily: "cron:form.mode.daily",
  weekly: "cron:form.mode.weekly",
  interval: "cron:form.mode.interval",
  custom: "cron:form.mode.custom",
}

export const INTERVAL_UNITS = ["minutes", "hours"] as const
export type IntervalUnit = (typeof INTERVAL_UNITS)[number]

export const INTERVAL_UNIT_KEYS: Record<IntervalUnit, string> = {
  minutes: "cron:form.unit.minutes",
  hours: "cron:form.unit.hours",
}

// 0 = Sunday, matching cron day-of-week numbering.
export const WEEKDAY_KEYS: Record<number, string> = {
  0: "cron:weekday.sun",
  1: "cron:weekday.mon",
  2: "cron:weekday.tue",
  3: "cron:weekday.wed",
  4: "cron:weekday.thu",
  5: "cron:weekday.fri",
  6: "cron:weekday.sat",
}

export const RUN_STATUS_KEYS: Record<string, string> = {
  ok: "cron:run.status.ok",
  error: "cron:run.status.error",
  skipped: "cron:run.status.skipped",
  running: "cron:run.status.running",
}

/** Backend's silence sentinel: such runs are recorded but never injected. */
export const SILENT_SENTINEL = "NO_REPLY"

/** Mirrors backend cron_min_interval_seconds (5 minutes). */
export const MIN_INTERVAL_MINUTES = 5

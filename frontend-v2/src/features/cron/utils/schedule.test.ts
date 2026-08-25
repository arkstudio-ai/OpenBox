import { describe, expect, it } from "vitest"
import {
  DEFAULT_FORM,
  buildSchedule,
  describeSchedule,
  intervalMs,
  isSilentResult,
  scheduleToForm,
} from "./schedule"
import type { CronSchedule } from "@/features/cron/types"

const t = (key: string, options?: Record<string, unknown>) =>
  options ? `${key}|${JSON.stringify(options)}` : key

describe("buildSchedule", () => {
  it("builds a daily cron with the given timezone", () => {
    const s = buildSchedule({ ...DEFAULT_FORM, mode: "daily", time: "09:30" }, "Asia/Shanghai")
    expect(s).toEqual({ kind: "cron", expr: "30 9 * * *", tz: "Asia/Shanghai" })
  })

  it("builds a weekly cron", () => {
    const s = buildSchedule({ ...DEFAULT_FORM, mode: "weekly", time: "08:00", weekday: 5 }, "UTC")
    expect(s).toEqual({ kind: "cron", expr: "0 8 * * 5", tz: "UTC" })
  })

  it("builds interval schedules in ms", () => {
    expect(buildSchedule({ ...DEFAULT_FORM, mode: "interval", every: 30, unit: "minutes" }, "UTC")).toEqual({
      kind: "every",
      every_ms: 30 * 60_000,
    })
    expect(buildSchedule({ ...DEFAULT_FORM, mode: "interval", every: 2, unit: "hours" }, "UTC")).toEqual({
      kind: "every",
      every_ms: 2 * 3_600_000,
    })
  })

  it("refuses sub-minimum intervals and invalid input", () => {
    expect(buildSchedule({ ...DEFAULT_FORM, mode: "interval", every: 2, unit: "minutes" }, "UTC")).toBeNull()
    expect(buildSchedule({ ...DEFAULT_FORM, mode: "interval", every: 0, unit: "hours" }, "UTC")).toBeNull()
    expect(buildSchedule({ ...DEFAULT_FORM, mode: "daily", time: "25:99" }, "UTC")).toBeNull()
    expect(buildSchedule({ ...DEFAULT_FORM, mode: "custom", expr: "bad" }, "UTC")).toBeNull()
  })

  it("passes custom cron expressions through", () => {
    expect(buildSchedule({ ...DEFAULT_FORM, mode: "custom", expr: "*/30 9-18 * * 1-5" }, "UTC")).toEqual({
      kind: "cron",
      expr: "*/30 9-18 * * 1-5",
      tz: "UTC",
    })
  })
})

describe("scheduleToForm", () => {
  it("round-trips daily and weekly expressions", () => {
    expect(scheduleToForm({ kind: "cron", expr: "30 9 * * *" }).time).toBe("09:30")
    const weekly = scheduleToForm({ kind: "cron", expr: "0 8 * * 5" })
    expect(weekly.mode).toBe("weekly")
    expect(weekly.weekday).toBe(5)
  })

  it("recognizes intervals in both units", () => {
    expect(scheduleToForm({ kind: "every", every_ms: 7_200_000 })).toMatchObject({ every: 2, unit: "hours" })
    expect(scheduleToForm({ kind: "every", every_ms: 900_000 })).toMatchObject({ every: 15, unit: "minutes" })
  })

  it("falls back to custom for irregular expressions", () => {
    expect(scheduleToForm({ kind: "cron", expr: "*/10 * * * 1-5" }).mode).toBe("custom")
  })
})

describe("describeSchedule", () => {
  const cases: Array<[CronSchedule, string]> = [
    [{ kind: "cron", expr: "0 9 * * *" }, 'cron:describe.daily|{"time":"09:00"}'],
    [{ kind: "every", every_ms: 1_800_000 }, 'cron:describe.everyMinutes|{"count":30}'],
    [{ kind: "every", every_ms: 3_600_000 }, 'cron:describe.everyHours|{"count":1}'],
    [{ kind: "cron", expr: "*/7 * * * *" }, 'cron:describe.cron|{"expr":"*/7 * * * *"}'],
    [{ kind: "at", at: "2026-09-01T00:00:00Z" }, 'cron:describe.once|{"time":"2026-09-01T00:00:00Z"}'],
  ]
  it.each(cases)("describes %j", (schedule, expected) => {
    expect(describeSchedule(schedule, t)).toBe(expected)
  })

  it("names the weekday for weekly schedules", () => {
    expect(describeSchedule({ kind: "cron", expr: "0 8 * * 5" }, t)).toContain("cron:weekday.fri")
  })
})

describe("isSilentResult", () => {
  it.each([null, "", "  ", "NO_REPLY", " NO_REPLY \n", "**NO_REPLY**"])("silent: %j", (v) => {
    expect(isSilentResult(v)).toBe(true)
  })
  it.each(["done", "NO_REPLY but with a long trailing explanation"])("loud: %j", (v) => {
    expect(isSilentResult(v)).toBe(false)
  })
})

describe("intervalMs", () => {
  it("converts both units", () => {
    expect(intervalMs(5, "minutes")).toBe(300_000)
    expect(intervalMs(1, "hours")).toBe(3_600_000)
  })
})

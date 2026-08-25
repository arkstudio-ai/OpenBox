import { describe, expect, it } from "vitest"
import { summarize } from "./CronStatusPill"
import type { CronJob } from "@/features/cron/types"

function job(over: Partial<CronJob>): CronJob {
  return {
    id: "j",
    user_id: "u",
    project_id: "p",
    session_id: "s",
    name: "n",
    description: "",
    enabled: true,
    schedule: { kind: "every", every_ms: 600_000 },
    task_prompt: "p",
    agent: "build",
    model: null,
    timeout_seconds: 1800,
    delivery: {},
    delete_after_run: false,
    next_run_at: null,
    last_run_at: null,
    last_status: null,
    last_error: null,
    last_duration_ms: null,
    consecutive_errors: 0,
    total_runs: 0,
    total_successes: 0,
    total_failures: 0,
    running: false,
    created_at: null,
    updated_at: null,
    ...over,
  }
}

describe("summarize", () => {
  it("picks the earliest next run among enabled jobs only", () => {
    const s = summarize([
      job({ next_run_at: "2026-08-26T09:00:00Z" }),
      job({ next_run_at: "2026-08-26T07:00:00Z" }),
      job({ enabled: false, next_run_at: "2026-08-26T01:00:00Z" }),
    ])
    expect(s.nextRun).toBe("2026-08-26T07:00:00Z")
  })

  it("flags running and counts failures", () => {
    const s = summarize([
      job({ running: true }),
      job({ last_status: "error" }),
      job({ last_status: "error", last_error: "[auto-disabled after 10 consecutive failures] x" }),
    ])
    expect(s.running).toBe(true)
    expect(s.failedCount).toBe(2)
    expect(s.autoDisabled).toBe(true)
  })

  it("takes the most recent last run across jobs", () => {
    const s = summarize([
      job({ last_run_at: "2026-08-25T01:00:00Z" }),
      job({ last_run_at: "2026-08-25T09:00:00Z" }),
    ])
    expect(s.lastRun).toBe("2026-08-25T09:00:00Z")
  })

  it("handles the empty shape", () => {
    const s = summarize([])
    expect(s.nextRun).toBeUndefined()
    expect(s.lastRun).toBeUndefined()
    expect(s.failedCount).toBe(0)
  })
})

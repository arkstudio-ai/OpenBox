import { describe, expect, it } from "vitest"
import { resultSummary, skillDisplayName, statusTone } from "./SkillJobCard"
import { visibleJobs } from "./SkillJobsDock"
import type { SkillJobSnapshot } from "@/features/jobs/types"

function job(overrides: Partial<SkillJobSnapshot>): SkillJobSnapshot {
  return {
    jobId: "j1",
    skillKey: "builtin:demo-echo",
    operation: "echo",
    status: "queued",
    phase: null,
    phaseLabelKey: null,
    desiredState: "run",
    progress: {},
    result: {},
    errorCode: null,
    errorMessage: null,
    attempt: 0,
    retryCount: 0,
    maxAttempts: 3,
    externalWaitSeconds: 0,
    maxExternalWaitSeconds: 86400,
    sessionId: "s1",
    queue: "default",
    lastEventSeq: 1,
    nextRunAt: null,
    deadlineAt: null,
    createdAt: null,
    updatedAt: null,
    completedAt: null,
    ...overrides,
  }
}

describe("statusTone", () => {
  it("maps every status to a tone", () => {
    const statuses: SkillJobSnapshot["status"][] = [
      "queued", "running", "waiting_external", "waiting_user", "waiting_agent",
      "retry_scheduled", "succeeded", "failed", "cancelled",
    ]
    for (const status of statuses) {
      const tone = statusTone(status)
      expect(tone.dot).toMatch(/^bg-/)
      expect(tone.labelKey).toContain("status.")
    }
  })

  it("failed is the only danger tone", () => {
    expect(statusTone("failed").dot).toBe("bg-danger")
    expect(statusTone("succeeded").dot).toBe("bg-sage")
  })
})

describe("skillDisplayName", () => {
  it("strips the distribution prefix", () => {
    expect(skillDisplayName("builtin:demo-echo")).toBe("demo-echo")
    expect(skillDisplayName("user:my-script")).toBe("my-script")
  })
})

describe("visibleJobs", () => {
  const now = Date.parse("2026-08-28T12:00:00Z")

  it("keeps active jobs and recent terminal ones only", () => {
    const jobs = [
      job({ jobId: "active", status: "waiting_external" }),
      job({ jobId: "fresh-done", status: "succeeded", completedAt: "2026-08-28T11:55:00Z" }),
      job({ jobId: "old-done", status: "succeeded", completedAt: "2026-08-28T10:00:00Z" }),
    ]
    expect(visibleJobs(jobs, now).map((j) => j.jobId)).toEqual(["active", "fresh-done"])
  })

  it("caps terminal jobs at three", () => {
    const jobs = [1, 2, 3, 4, 5].map((n) =>
      job({ jobId: `done-${n}`, status: "failed", completedAt: "2026-08-28T11:59:00Z" }),
    )
    expect(visibleJobs(jobs, now)).toHaveLength(3)
  })

  it("empty when nothing is active or recent", () => {
    expect(visibleJobs([job({ jobId: "x", status: "cancelled", completedAt: null })], now)).toEqual([])
  })
})


describe("resultSummary", () => {
  it("hides identifiers — they address things, they do not inform", () => {
    // A finished video job returns exactly this shape. Printing it made the
    // card read as a debug dump while the video sat right underneath.
    expect(
      resultSummary({
        asset_id: "asset_1",
        segment_id: "segment_1",
        video_job_id: "video_1",
        production_id: "production_1",
        provider_task_id: "task_1",
      }),
    ).toBeNull()
  })

  it("keeps values a reader can actually use", () => {
    expect(resultSummary({ echo: "hello", asset_id: "asset_1" })).toBe("echo: hello")
  })

  it("catches camelCase ids too", () => {
    expect(resultSummary({ jobId: "j1", assetId: "a1" })).toBeNull()
  })
})

describe("visibleJobs · receipt de-duplication", () => {
  const now = Date.parse("2026-08-28T12:00:00Z")

  it("drops a finished job once the transcript shows its receipt", () => {
    const jobs = [job({ jobId: "done", status: "succeeded", completedAt: "2026-08-28T11:59:00Z" })]
    expect(visibleJobs(jobs, now, new Set(["done"]))).toEqual([])
  })

  it("keeps a finished job that has no receipt yet", () => {
    // The receipt write is a moment behind the status flip, and it can be
    // switched off entirely — a result must never be nowhere.
    const jobs = [job({ jobId: "done", status: "succeeded", completedAt: "2026-08-28T11:59:00Z" })]
    expect(visibleJobs(jobs, now, new Set()).map((j) => j.jobId)).toEqual(["done"])
  })

  it("never hides a job that is still running", () => {
    const jobs = [job({ jobId: "live", status: "running" })]
    expect(visibleJobs(jobs, now, new Set(["live"])).map((j) => j.jobId)).toEqual(["live"])
  })
})

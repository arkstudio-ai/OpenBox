import { describe, expect, it } from "vitest"
import { pickRun } from "./jobs"
import type { CronRun } from "@/features/cron/types"

function run(id: string, sessionId: string | null): CronRun {
  return {
    id,
    job_id: "j",
    temp_session_id: sessionId,
    status: "ok",
    error_message: null,
    task_prompt: null,
    summary_text: null,
    injected: false,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    duration_ms: 0,
    started_at: null,
    ended_at: null,
  }
}

describe("pickRun", () => {
  const runs = [run("skipped", null), run("newest", "s2"), run("older", "s1")]

  it("opens the run the URL names", () => {
    expect(pickRun(runs, "older")?.id).toBe("older")
  })

  it("falls back to the newest run with a transcript", () => {
    expect(pickRun(runs, null)?.id).toBe("newest")
    expect(pickRun(runs, "gone")?.id).toBe("newest")
  })

  it("settles for the newest run when none left a transcript", () => {
    expect(pickRun([run("a", null), run("b", null)], null)?.id).toBe("a")
    expect(pickRun([], null)).toBeNull()
  })
})

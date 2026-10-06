import { describe, expect, it } from "vitest"
import type { SessionStatus } from "@/shared/types/api"
import { watchState } from "./watch-state"

const state = (session_status: SessionStatus, observed_state: string, pending_questions = 0) =>
  watchState({ session_status, observed_state, pending_questions })

describe("watch list status dot", () => {
  it("puts a question waiting on the user first", () => {
    expect(state("busy", "running", 1)).toBe("waiting_input")
    expect(state("waiting_input", "running")).toBe("waiting_input")
    expect(state("idle", "waiting_input")).toBe("waiting_input")
  })

  it.each(["busy", "finalizing", "retry", "compacting"] as const)("treats a %s conversation as running", (status) => {
    expect(state(status, "completed")).toBe("running")
  })

  it("prefers the live session status over the task's last record", () => {
    expect(state("queued", "completed")).toBe("queued")
    expect(state("error", "completed")).toBe("error")
  })

  it.each([
    ["running", "running"], ["queued", "queued"], ["completed", "completed"],
    ["error", "error"], ["resume_blocked", "error"], ["effect_unknown", "error"],
    ["idle", "idle"], ["paused", "idle"], ["canceled", "idle"], ["aborted", "idle"], ["anything-new", "idle"],
  ])("reads a quiet conversation's task state %s as %s", (observed, expected) => {
    expect(state("idle", observed)).toBe(expected)
  })
})

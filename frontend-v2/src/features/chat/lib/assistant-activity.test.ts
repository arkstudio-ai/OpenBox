import { describe, expect, it } from "vitest"
import type { ToolPart } from "@/shared/types/api"
import { assistantActivity } from "./assistant-activity"

const call = (tool: string, status: ToolPart["status"] = "running") =>
  ({ id: tool, type: "tool", tool, status, input: {} }) as unknown as ToolPart

describe("what the assistant is doing, in words", () => {
  it.each([
    ["tasks.submit", "delegating"],
    ["tasks.pause", "updatingTask"],
    ["results.read", "checkingWork"],
    ["memory.remember", "remembering"],
    ["memory.search", "recalling"],
    ["knowledge.read", "reading"],
    ["requests.list", "checkingRequests"],
    ["briefing.configure", "scheduling"],
    ["projects.brief.update", "updatingBrief"],
    ["assets.attach", "handlingFiles"],
    ["status.credits", "checkingStatus"],
    ["something.new", "working"],
  ])("%s reads as %s", (tool, expected) => {
    expect(assistantActivity([call(tool)])).toBe(expected)
  })

  it("names the call in flight over the last finished one, and thinks before any call", () => {
    expect(assistantActivity([call("tasks.list", "running"), call("memory.search", "completed")])).toBe("checkingWork")
    expect(assistantActivity([call("tasks.list", "completed"), call("memory.search", "completed")])).toBe("recalling")
    expect(assistantActivity([])).toBe("thinking")
  })
})

import { expect, it } from "vitest"
import type { ToolPart } from "@/shared/types/api"
import { describeTool, toolTarget } from "./tool-map"

const part = (tool: string, input: Record<string, unknown>, title: string) =>
  ({ type: "tool", tool, input, title, status: "completed" }) as unknown as ToolPart

it("labels memory tools in the reader's language, never with their English result title", () => {
  expect(describeTool("memory_search").kindKey).toBe("memorySearch")
  expect(describeTool("memory_forget").kindKey).toBe("memoryForget")
  expect(describeTool("creator_context").kindKey).toBe("memory")
  expect(toolTarget(part("memory_search", { query: "菠萝 过敏" }, "Memory evidence"))).toBe("菠萝 过敏")
  expect(
    toolTarget(part("creator_context", { action: "write_memory" }, "Memory processing in background")),
  ).toBe("")
  expect(toolTarget(part("memory_forget", { memory_id: "mem_1" }, "Waiting for your answer"))).toBe("")
})

import { describe, expect, it } from "vitest"
import { resolveToolLayout } from "./tool-map"

describe("resolveToolLayout", () => {
  it("reads a desktop takeover back as the question it filed", () => {
    expect(resolveToolLayout("desktop_takeover")).toBe("question")
    expect(resolveToolLayout("question")).toBe("question")
  })

  it("keeps MCP and unknown tools generic", () => {
    expect(resolveToolLayout("mcp_douyin_search")).toBe("generic")
    expect(resolveToolLayout("hot_trends")).toBe("generic")
  })
})

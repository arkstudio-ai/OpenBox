import { describe, expect, it } from "vitest"
import type { McpServer } from "@/features/skills-center/types"
import { mcpServerDescription } from "./MineList"

function server(overrides: Partial<McpServer>): McpServer {
  return {
    name: "acceptance",
    type: "stdio",
    status: "connected",
    tools: [],
    resources: [],
    prompts: [],
    error: null,
    ...overrides,
  }
}

describe("MCP command display", () => {
  it("shows project-scoped arguments as Unicode-safe relative paths", () => {
    const root = "/workspace/openbox/users/u-f92e166792b015ce7389/projects/p-e2b7f7faa8325b77765d-demo"
    expect(
      mcpServerDescription(
        server({ command: "python3", args: [`${root}/工具/中文服务.py`, "--quiet"] }),
      ),
    ).toBe("python3 工具/中文服务.py --quiet")
  })

  it("does not rewrite remote URLs or ordinary relative arguments", () => {
    expect(mcpServerDescription(server({ url: "https://mcp.example.test/path" }))).toBe(
      "https://mcp.example.test/path",
    )
    expect(mcpServerDescription(server({ command: "npx", args: ["-y", "server-memory"] }))).toBe(
      "npx -y server-memory",
    )
  })
})

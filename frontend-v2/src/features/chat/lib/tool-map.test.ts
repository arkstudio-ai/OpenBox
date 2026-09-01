import { describe, expect, it } from "vitest"
import type { ToolPart } from "@/shared/types/api"
import { previewInput, toolTarget } from "./tool-map"

const ROOT = "/workspace/openbox/users/u-a/projects/p-b-demo"

describe("project-relative tool consumers", () => {
  it("renders file targets without the physical workspace namespace", () => {
    const part: ToolPart = {
      type: "tool",
      id: "tool-1",
      tool: "read",
      status: "completed",
      input: { file_path: `${ROOT}/资料/你好😀.txt` },
      title: `Error reading ${ROOT}/资料/你好😀.txt`,
    }

    expect(toolTarget(part)).toBe("资料/你好😀.txt")
  })

  it("renders permission path and patch fields as project-relative", () => {
    const detail = previewInput({
      file_path: `${ROOT}/资料/你好😀.txt`,
      patch: `*** Update File: ${ROOT}/资料/你好😀.txt\n+正文`,
    })

    expect(detail).toContain('"file_path":"资料/你好😀.txt"')
    expect(detail).toContain("*** Update File: 资料/你好😀.txt")
    expect(detail).not.toContain("/workspace/openbox/users")
  })
})

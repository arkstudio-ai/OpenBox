import { describe, expect, it } from "vitest"
import { fileSearchPath } from "./mention"

describe("fileSearchPath", () => {
  it("carries the project scope and encodes Unicode opaque ids exactly once", () => {
    const path = fileSearchPath("desktop/中文", "设计 稿😀", "session/你好", "project + one")
    const url = new URL(path, "https://openbox.invalid")

    expect(decodeURIComponent(url.pathname)).toBe("/api/containers/desktop/中文/files/search")
    expect(url.searchParams.get("q")).toBe("设计 稿😀")
    expect(url.searchParams.get("session_id")).toBe("session/你好")
    expect(url.searchParams.get("project_id")).toBe("project + one")
  })
})

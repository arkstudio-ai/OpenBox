import { describe, expect, it } from "vitest"
import { projectScopedDisplayPath, projectScopedDisplayText, projectScopedToolText } from "./project-path"

const ROOT = "/workspace/openbox/users/u-a/projects/p-b-demo"
const UPLOAD = "/workspace/openbox/users/u-a/.openbox/uploads/p-b-demo/a-123/image 你好.jpg"

describe("project-facing persisted paths", () => {
  it("hides the physical namespace while preserving Unicode", () => {
    expect(projectScopedDisplayPath(`${ROOT}/资料/你好😀.txt`)).toBe("资料/你好😀.txt")
    expect(projectScopedDisplayPath(ROOT)).toBe(".")
    expect(projectScopedDisplayPath("users/资料/你好😀.txt")).toBe("users/资料/你好😀.txt")
    expect(projectScopedDisplayText(`${ROOT}/资料/你好.ts:3:命中\n${ROOT}/README.md`)).toBe(
      "资料/你好.ts:3:命中\nREADME.md",
    )
    expect(projectScopedDisplayPath(UPLOAD)).toBe(".openbox/uploads/a-123/image 你好.jpg")
    expect(projectScopedDisplayText(`asset_id=x; path=${UPLOAD}; image/png`)).toBe(
      "asset_id=x; path=.openbox/uploads/a-123/image 你好.jpg; image/png",
    )
  })

  it("decodes historical Git C-quoted UTF-8 paths without corrupting literals", () => {
    const oldGitPath =
      '"\\344\\270\\255\\346\\226\\207\\347\\233\\256\\345\\275\\225/\\344\\275\\240\\345\\245\\275.txt"'

    expect(projectScopedDisplayPath(oldGitPath)).toBe("中文目录/你好.txt")
    expect(projectScopedDisplayPath('"literal-quotes.txt"')).toBe('"literal-quotes.txt"')
    expect(projectScopedDisplayPath('"\\999-invalid"')).toBe('"\\999-invalid"')
  })

  it("rewrites path protocol lines without touching code text", () => {
    expect(projectScopedToolText(`*** Update File: ${ROOT}/资料/你好😀.txt\n+正文`)).toBe(
      "*** Update File: 资料/你好😀.txt\n+正文",
    )
    expect(projectScopedToolText(`Updated ${ROOT}/资料/你好😀.txt`)).toBe("Updated 资料/你好😀.txt")
  })
})

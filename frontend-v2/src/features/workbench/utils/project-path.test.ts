import { describe, expect, it } from "vitest"
import { projectParentPath, projectRelativePath, resolveProjectPath } from "./project-path"

const ROOT = "/workspace/openbox/users/u-a/projects/p-b-demo"

describe("project file paths", () => {
  it("renders nested Unicode paths relative to the project", () => {
    const absolute = `${ROOT}/资料/设计稿-你好.md`

    expect(projectRelativePath(absolute, ROOT)).toBe("资料/设计稿-你好.md")
    expect(resolveProjectPath("资料/设计稿-你好.md", ROOT)).toBe(absolute)
  })

  it("never accepts or renders a physical path outside the project", () => {
    expect(resolveProjectPath("/workspace/openbox/users/u-a/projects/p-other/secret", ROOT)).toBeNull()
    expect(projectRelativePath("/workspace/openbox/users/u-a/projects/p-other/secret", ROOT)).toBeNull()
    expect(resolveProjectPath("../secret", ROOT)).toBeNull()
  })

  it("keeps an already absolute selection only when it is inside the project", () => {
    expect(resolveProjectPath(`${ROOT}/src/app.ts`, `${ROOT}/`)).toBe(`${ROOT}/src/app.ts`)
    expect(projectRelativePath(ROOT, ROOT)).toBe(".")
    expect(projectParentPath(ROOT, ROOT)).toBe(ROOT)
    expect(projectParentPath(`${ROOT}/资料/你好.txt`, ROOT)).toBe(`${ROOT}/资料`)
    expect(projectParentPath("/workspace/openbox/users/u-a/projects", ROOT)).toBe(ROOT)
  })
})

import { describe, expect, it } from "vitest"
import { versionChanges } from "./version-diff"

describe("definition version comparison", () => {
  it("ignores object-key ordering while retaining meaningful array order", () => {
    expect(
      versionChanges(
        { policy: { limit: 3, enabled: true }, members: ["writer", "reviewer"] },
        { policy: { enabled: true, limit: 3 }, members: ["reviewer", "writer"] },
      ),
    ).toEqual([{ key: "members", before: ["writer", "reviewer"], after: ["reviewer", "writer"] }])
  })

  it("distinguishes removed, null, false, zero and empty values", () => {
    expect(
      versionChanges(
        { old: "x", flag: false, count: 0, model: null },
        { flag: null, count: "", model: "test", added: false },
      ),
    ).toEqual([
      { key: "old", before: "x", after: undefined },
      { key: "flag", before: false, after: null },
      { key: "count", before: 0, after: "" },
      { key: "model", before: null, after: "test" },
      { key: "added", before: undefined, after: false },
    ])
  })
})

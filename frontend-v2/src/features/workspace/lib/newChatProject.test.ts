import { describe, expect, it } from "vitest"
import type { Project } from "@/shared/types/api"
import { resolveNewChatProject } from "./newChatProject"

const projects = [
  { id: "p1", name: "Alpha" },
  { id: "p2", name: "Beta" },
] as Project[]

describe("resolveNewChatProject", () => {
  it("lets the URL win over the sidebar selection", () => {
    expect(resolveNewChatProject({ requested: "p2", selected: "p1", projects })).toEqual({
      projectId: "p2",
      projectName: "Beta",
    })
  })

  it("falls back to the sidebar's current project", () => {
    expect(resolveNewChatProject({ requested: null, selected: "p1", projects })).toEqual({
      projectId: "p1",
      projectName: "Alpha",
    })
  })

  it("ignores a selection whose project no longer exists", () => {
    expect(resolveNewChatProject({ requested: null, selected: "gone", projects })).toEqual({})
  })

  it("files under nothing when neither is set", () => {
    expect(resolveNewChatProject({ requested: null, selected: null, projects })).toEqual({})
  })
})

import { describe, expect, it } from "vitest"
import { groupSkills } from "./group-skills"
import type { InstalledSkill } from "@/features/skills-center/types"

function skill(name: string, install_dir?: string, source = "container"): InstalledSkill {
  return { name, install_dir, source, description: `${name} desc` }
}

// Cloning anthropic/skills lands 19 SKILL.md files under one directory. The
// scan reports 19 entries sharing one install_dir, and uninstall is addressed
// by that directory — so the flat list both overstated what was installed and
// hid that removing one row removes all of them.
describe("groupSkills", () => {
  it("collapses one pack install into a single row", () => {
    const pack = ["docx", "pdf", "pptx"].map((n) => skill(n, "anthropic-skills"))
    const groups = groupSkills(pack)
    expect(groups).toHaveLength(1)
    expect(groups[0].id).toBe("anthropic-skills")
    expect(groups[0].isPack).toBe(true)
    expect(groups[0].members).toHaveLength(3)
  })

  it("counts installs, not skills", () => {
    const groups = groupSkills([
      ...["docx", "pdf"].map((n) => skill(n, "anthropic-skills")),
      skill("changelog-writer", "changelog-writer"),
    ])
    expect(groups).toHaveLength(2)
  })

  it("leaves a single-skill install alone", () => {
    const [group] = groupSkills([skill("changelog-writer", "changelog-writer")])
    expect(group.isPack).toBe(false)
    expect(group.name).toBe("changelog-writer")
    expect(group.description).toBe("changelog-writer desc")
  })

  it("names a pack by its directory, since no member describes the whole", () => {
    const [group] = groupSkills(["a", "b"].map((n) => skill(n, "the-pack")))
    expect(group.name).toBe("the-pack")
  })

  it("groups host skills that have no install directory by their own name", () => {
    const groups = groupSkills([skill("scheduled-tasks", undefined, "project")])
    expect(groups[0].id).toBe("scheduled-tasks")
    expect(groups[0].removable).toBe(false)
  })

  it("marks a group removable only when the sandbox owns it", () => {
    const [container] = groupSkills([skill("mine", "mine")])
    const [host] = groupSkills([skill("theirs", undefined, "project")])
    expect(container.removable).toBe(true)
    expect(host.removable).toBe(false)
  })

  it("puts packs first so the biggest install is easiest to find", () => {
    const groups = groupSkills([
      skill("zebra", "zebra"),
      ...["a", "b"].map((n) => skill(n, "pack")),
      skill("alpha", "alpha"),
    ])
    expect(groups.map((g) => g.id)).toEqual(["pack", "alpha", "zebra"])
  })
})

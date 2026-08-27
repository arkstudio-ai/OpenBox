import { describe, expect, it } from "vitest"
import { groupSkills } from "./group-skills"
import type { InstalledSkill } from "@/features/skills-center/types"

function skill(
  name: string,
  install_dir?: string,
  source = "container",
  extra: Partial<InstalledSkill> = {},
): InstalledSkill {
  return { name, install_dir, source, description: `${name} desc`, ...extra }
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

  it("names where a group came from, so the row can say so", () => {
    const [container] = groupSkills([skill("mine", "mine")])
    const [builtin] = groupSkills([skill("dev-browser", "dev-browser", "builtin")])
    const [host] = groupSkills([skill("scheduled-tasks", undefined, "project")])
    expect(container.origin).toBe("container")
    expect(builtin.origin).toBe("builtin")
    expect(host.origin).toBe("host")
  })

  it("keeps builtin skills in the list, just not removable", () => {
    // Hiding them made this an inventory of what is removable rather than of
    // what the agent actually has.
    const [group] = groupSkills([skill("dev-browser", "dev-browser", "builtin")])
    expect(group.removable).toBe(false)
    expect(group.members).toHaveLength(1)
  })

  it("keeps personal publication state on the install group", () => {
    const [group] = groupSkills([
      skill("my-writer", "my-writer", "container", {
        category: "personal",
        publication_status: "unpublished",
        library_id: "library-1",
      }),
    ])
    expect(group.category).toBe("personal")
    expect(group.publicationStatus).toBe("unpublished")
    expect(group.libraryId).toBe("library-1")
  })

  it("does not mistake a store install for a personal skill", () => {
    const [group] = groupSkills([
      skill("shared-writer", "shared-writer", "container", {
        category: "store",
        publication_status: null,
        catalog_id: "community-1",
      }),
    ])
    expect(group.category).toBe("store")
    expect(group.publicationStatus).toBeNull()
    expect(group.catalogId).toBe("community-1")
  })

  it("publishes a multi-skill archive as one personal install", () => {
    const members = ["writer", "reviewer"].map((name) =>
      skill(name, "my-pack", "container", {
        category: "personal",
        publication_status: "published",
        catalog_id: "community-pack",
      }),
    )
    const [group] = groupSkills(members)
    expect(group.id).toBe("my-pack")
    expect(group.category).toBe("personal")
    expect(group.publicationStatus).toBe("published")
    expect(group.catalogId).toBe("community-pack")
  })
})

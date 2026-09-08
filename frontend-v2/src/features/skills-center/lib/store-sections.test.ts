import { describe, expect, it } from "vitest"
import { buildStoreShelves } from "./store-sections"
import type { CatalogMcp, CatalogSkill, StoreOrigin } from "@/features/skills-center/types"

function skill(id: string, extra: Partial<CatalogSkill> = {}): CatalogSkill {
  return {
    id,
    kind: "skill",
    name: id,
    title: id,
    icon: "",
    description: `${id} desc`,
    installed: false,
    requires_mcp: [],
    missing_mcp: [],
    install: {},
    ...extra,
  }
}

function mcp(id: string, extra: Partial<CatalogMcp> = {}): CatalogMcp {
  return {
    id,
    kind: "mcp",
    name: id,
    title: id,
    icon: "",
    description: `${id} desc`,
    installed: false,
    config: { type: "stdio" },
    ...extra,
  }
}

const shelvesOf = (result: ReturnType<typeof buildStoreShelves>): StoreOrigin[] =>
  result.sections.map((s) => s.origin)

describe("buildStoreShelves", () => {
  it("shelves entries by who published them, ours first", () => {
    const result = buildStoreShelves(
      {
        skills: [skill("theirs", { origin: "third_party" }), skill("ours", { origin: "official" })],
        mcp: [mcp("shared", { origin: "community" })],
      },
      { kind: "all", query: "" },
    )
    expect(shelvesOf(result)).toEqual(["official", "community", "third_party"])
  })

  it("treats an entry with no origin as a user upload, never as ours", () => {
    // An older backend sent no shelves at all. Guessing "official" would put a
    // stranger's work under our name.
    const result = buildStoreShelves({ skills: [skill("legacy")], mcp: [] }, { kind: "all", query: "" })
    expect(shelvesOf(result)).toEqual(["community"])
  })

  it("mixes both kinds onto one shelf, featured first", () => {
    const result = buildStoreShelves(
      {
        skills: [skill("popular", { origin: "official", installs_count: 40 })],
        mcp: [
          mcp("pinned", { origin: "official", featured: true }),
          mcp("quiet", { origin: "official", installs_count: 1 }),
        ],
      },
      { kind: "all", query: "" },
    )
    expect(result.sections[0].entries.map((e) => e.id)).toEqual(["pinned", "popular", "quiet"])
  })

  it("keeps the kind filter working across every shelf", () => {
    const result = buildStoreShelves(
      {
        skills: [skill("ours", { origin: "official" })],
        mcp: [mcp("theirs", { origin: "third_party" })],
      },
      { kind: "skill", query: "" },
    )
    expect(shelvesOf(result)).toEqual(["official"])
    expect(result.sections[0].entries).toHaveLength(1)
  })

  it("searches titles, descriptions, names and tags across shelves", () => {
    const result = buildStoreShelves(
      {
        skills: [skill("research", { origin: "official", tags: ["web"] })],
        mcp: [mcp("playwright", { origin: "third_party", description: "browser automation" })],
      },
      { kind: "all", query: "browser" },
    )
    expect(shelvesOf(result)).toEqual(["third_party"])
  })

  it("reports the unfiltered size so an empty search reads differently from an empty store", () => {
    const stocked = buildStoreShelves(
      { skills: [skill("a", { origin: "official" })], mcp: [] },
      { kind: "all", query: "nothing matches this" },
    )
    expect(stocked.sections).toHaveLength(0)
    expect(stocked.total).toBe(1)

    const bare = buildStoreShelves({ skills: [], mcp: [] }, { kind: "all", query: "" })
    expect(bare.total).toBe(0)
  })

  it("survives a catalogue that has not loaded yet", () => {
    expect(buildStoreShelves(undefined, { kind: "all", query: "" })).toEqual({
      sections: [],
      total: 0,
    })
  })
})

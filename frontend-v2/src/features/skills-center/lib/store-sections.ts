// Laying the store out by shelf instead of by type.
//
// Splitting the catalogue into "Skills" and "MCP servers" answered a question
// nobody asks first. What decides whether someone installs a thing is who
// stands behind it: us, another user, or a third party we merely host. So the
// shelves are the sections, and the kind is a filter across all of them —
// which is also what keeps a delisted third-party server from reading as ours.
import type { CatalogMcp, CatalogSkill, KindFilter, StoreOrigin } from "@/features/skills-center/types"

export type StoreEntry = CatalogSkill | CatalogMcp

export interface StoreSection {
  origin: StoreOrigin
  entries: StoreEntry[]
}

/** Ours first, then the people who use it, then everyone else. */
const SHELVES: readonly StoreOrigin[] = ["official", "community", "third_party"] as const

/** An entry from a backend that predates shelves is somebody's upload, not
 *  ours: guessing "official" would put a stranger's work under our name. */
function shelfOf(entry: StoreEntry): StoreOrigin {
  return entry.origin ?? "community"
}

function matches(query: string, entry: StoreEntry): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return [entry.title, entry.description, entry.name, entry.tags?.join(" ")].some((field) =>
    (field ?? "").toLowerCase().includes(q),
  )
}

/**
 * Featured first, then what people actually install, then alphabetically.
 *
 * The backend already orders each kind this way, but a shelf interleaves two
 * kinds, and concatenating two sorted lists is not a sorted list.
 */
function byShelfOrder(a: StoreEntry, b: StoreEntry): number {
  if (Boolean(a.featured) !== Boolean(b.featured)) return a.featured ? -1 : 1
  const installs = (b.installs_count ?? 0) - (a.installs_count ?? 0)
  if (installs !== 0) return installs
  return (a.title || a.name).localeCompare(b.title || b.name)
}

export interface StoreShelves {
  sections: StoreSection[]
  /** How many entries the store offers at all, before filter and search. */
  total: number
}

/**
 * Group the catalogue into the sections the store renders.
 *
 * `total` comes back alongside so the caller can tell "the store is empty"
 * from "your search matched nothing" — two different things to say, and only
 * one of them is the person's own doing.
 */
export function buildStoreShelves(
  catalog: { skills: CatalogSkill[]; mcp: CatalogMcp[] } | undefined,
  options: { kind: KindFilter; query: string },
): StoreShelves {
  const skills = catalog?.skills ?? []
  const mcp = catalog?.mcp ?? []
  const pool: StoreEntry[] = [
    ...(options.kind === "mcp" ? [] : skills),
    ...(options.kind === "skill" ? [] : mcp),
  ]
  const visible = pool.filter((entry) => matches(options.query, entry))

  const sections = SHELVES.map((origin) => ({
    origin,
    entries: visible.filter((entry) => shelfOf(entry) === origin).sort(byShelfOrder),
  })).filter((section) => section.entries.length > 0)

  return { sections, total: skills.length + mcp.length }
}

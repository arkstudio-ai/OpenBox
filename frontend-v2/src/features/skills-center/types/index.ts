// Skill centre domain types. Field names mirror the backend payloads
// (backend/api/metadata.py, backend/skill/catalog.py) — snake_case is kept
// rather than remapped so a field is searchable across both sides.

/** Which shelf of the store an entry sits on.
 *
 *  Absent on an older backend (and on the mobile client's payload), where the
 *  store had no shelves at all — everything it served was either ours or a
 *  user upload, so `community` is the honest default rather than `official`. */
export type StoreOrigin = "official" | "community" | "third_party"

/** The operator's decision about a published package.
 *
 *  Separate from the author's `publication_status`: the author says whether a
 *  release exists, the operator says whether it is on the shelf. A backend
 *  that predates review sends neither, and everything it published was listed. */
export type ListingState = "pending" | "listed" | "rejected" | "delisted"

/** An MCP server's transport. `stdio` runs a local process; `remote` is HTTP. */
export type McpTransport = "stdio" | "remote"

export interface McpConfig {
  type: McpTransport
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  headers?: Record<string, string>
  timeout?: number
}

/** An MCP server as the container reports it. */
export interface McpServer {
  name: string
  type: string
  status: "connected" | "disconnected" | "error"
  tools: { name: string; description?: string }[]
  resources: unknown[]
  prompts: unknown[]
  error: string | null
  command?: string | null
  args?: string[] | null
  url?: string | null
}

/** An installed skill. `icon`/`requires_mcp` come from SKILL.md frontmatter. */
export interface InstalledSkill {
  name: string
  description?: string
  icon?: string
  requires_mcp?: string[]
  homepage?: string
  /** "container" for user installs, "builtin" for image-baked, "global"/"project" for host. */
  source?: string
  install_dir?: string
  files?: string[]
  /** Product-facing origin. Unlike `source`, this distinguishes a user's own
   *  work from something they installed from the public store. */
  category?: "personal" | "store" | "installed" | "builtin" | "host"
  /** Only personal skills can be published. Built-in/store installs return null.
   *  `withdrawn` means the author pulled their own release; the snapshot the
   *  store held survives, so re-submitting it is one click. */
  publication_status?: "unpublished" | "published" | "withdrawn" | null
  /** Same value under the name the durable library row uses. Rows that never
   *  reached the sandbox come straight from the library and carry only this. */
  status?: "unpublished" | "published" | "withdrawn"
  /** Where the operator put this release. Null until there is one to place. */
  listing?: ListingState | null
  /** Why it was rejected or delisted — the author is owed the reason. */
  listing_note?: string | null
  /** Published while the author was an admin: the store's own editorial act. */
  is_official?: boolean
  /** Pinned to the top of its shelf. */
  featured?: boolean
  /** Durable library record backing a personal install. */
  library_id?: string | null
  /** Public catalogue record after publication, or the source catalogue entry for a store install. */
  catalog_id?: string | null
  published_at?: string | null
}

export interface CatalogEnvField {
  key: string
  label: string
  secret?: boolean
}

interface CatalogBase {
  id: string
  name: string
  title: string
  icon: string
  description: string
  publisher?: string
  homepage?: string
  tags?: string[]
  installed: boolean
  /** Store-wide key: `skill:web-research`, `mcp:playwright`, `community:7`. */
  catalog_id?: string
  origin?: StoreOrigin
  official?: boolean
  /** Pinned to the top of its shelf, and marked as such on the row. */
  featured?: boolean
  installs_count?: number
}

export interface CatalogMcp extends CatalogBase {
  kind: "mcp"
  config: McpConfig
  required_env?: CatalogEnvField[]
}

export interface CatalogSkill extends CatalogBase {
  kind: "skill"
  /** Published by a user rather than maintained in the operator catalogue. */
  community?: boolean
  requires_mcp: string[]
  /** Dependencies not yet installed — resolved server-side so both tabs agree. */
  missing_mcp: string[]
  install: { url?: string; name?: string; content?: string }
}

export interface Catalog {
  skills: CatalogSkill[]
  mcp: CatalogMcp[]
}

export interface CatalogInstallResult {
  ok: boolean
  installed: {
    kind: "skill" | "mcp"
    id: string
    name: string
    status: string
    error?: string | null
  }[]
}

/** The slice of `/api/agent/config` the centre depends on.
 *
 *  Only the review switch: the publish dialog promises either "an admin will
 *  look at this" or "everyone can see it now", and guessing wrong makes the
 *  product lie to the person clicking. */
export interface StoreConfig {
  skill_store_review?: boolean
}

/** Which half of the centre is showing. */
export type CenterTab = "mine" | "store"

/** Which kind of thing the current filter is limited to. */
export type KindFilter = "all" | "skill" | "mcp"

// Admin skill-store domain types. Field names mirror the backend payloads
// (backend/api/admin_skills.py) — snake_case is kept rather than remapped so a
// field is searchable across both sides.

/** Where an entry came from; drives the store's three sections. */
export type SkillOrigin = "official" | "community" | "third_party"

export type SkillKind = "skill" | "mcp"

/** The operator-side state machine (plan §4.4). */
export type SkillListing = "pending" | "listed" | "rejected" | "delisted"

export interface SkillAuthor {
  username: string
  email: string
}

/** One row of the merged catalogue + community view. */
export interface StoreEntry {
  /** `community:<row>` or `<kind>:<id>` — always contains a colon, so encode it into paths. */
  catalog_id: string
  kind: SkillKind
  origin: SkillOrigin
  title: string
  name: string
  icon?: string | null
  description?: string | null
  /** Catalogue entries carry a publisher; community rows carry an author. */
  publisher?: string | null
  author?: SkillAuthor | null
  workspace_id?: string | null
  version?: number | string | null
  published_at?: string | null
  installs_count: number
  listing: SkillListing
  listing_note?: string | null
  featured: boolean
  is_official: boolean
  requires_mcp?: string[] | null
  size?: number | null
  sha256?: string | null
  deleted?: boolean
}

export interface StoreDetail extends StoreEntry {
  revision: number
  content: string
  config?: Record<string, unknown>
  has_archive?: boolean
}

export interface Desktop {
  desktop_id: string
  workspace_id: string | null
  workspace_name: string | null
  username: string | null
  status: string
  channel_state: string | null
  members: { id: string; username: string; email: string }[]
}

export interface DesktopSkill {
  name: string
  names?: string[]
  kind: SkillKind
  install_dir: string
  description: string
  source: string
  icon: string
  removable: boolean
}

export interface DesktopScan {
  items: DesktopSkill[]
  unavailable: SkillKind[]
  desktop_id: string
  user_id: string
  scanned_at: string
}

export interface Page<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

export interface ReviewFile {
  path: string
  size: number
}

/** Review detail: the store row plus what the ZIP holds, read without unpacking. */
export interface ReviewDetail extends StoreEntry {
  /** Plain text, capped server-side. Rendered as text — never as Markdown or HTML. */
  skill_md: string | null
  files: ReviewFile[]
  /** Set when the server hit its 64 KB / 500-entry caps (plan §4.9). */
  skill_md_truncated?: boolean
  files_truncated?: boolean
  /**
   * Set when the server would not open the archive at all — over the size cap,
   * absurdly many declared members, or not a readable ZIP. It is the difference
   * between "nobody read these bytes" and "this package is empty", and the
   * reviewer is the one person who must not be told the second when it is the
   * first: `skill_md` is null and `files` empty either way.
   */
  archive_error?: string | null
  /** The archive opened fine; it just holds no SKILL.md. */
  skill_md_error?: string | null
  /** Members the archive declares — `files` stops at the server's listing cap. */
  files_total?: number
}

/** One "user X installed skill Y" record. */
export interface InstallRecord {
  /** The `skill_installs` row id — the only field guaranteed unique per row. */
  id: string
  user: SkillAuthor
  catalog_id: string
  kind: SkillKind
  title: string
  install_dir: string
  installed_at: string
  /** Present only when the server can resolve it for a catalogue entry. */
  icon?: string | null
  origin?: SkillOrigin | null
}

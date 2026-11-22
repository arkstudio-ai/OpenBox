// Wire types of the example library (backend/api/admin_examples.py, light/taxonomy.json).

export const ENTRY_IDS = ["video", "menu", "poster", "footage_edit"] as const
export type EntryId = (typeof ENTRY_IDS)[number]
export type ExampleStatus = "active" | "disabled"

export interface AssetRef {
  asset_id: string
  oss_key?: string | null
  name?: string | null
  mime?: string | null
}

export interface ExampleSource {
  session_id?: string | null
  job_id?: string | null
  user_id?: string | null
  workspace_id?: string | null
  shop_name?: string | null
  headline?: string | null
}

/** How the reference card came to be; no `state` while nothing has been read yet. */
export interface CardState {
  state?: "reading" | "ready" | "failed" | "edited"
  at?: string
  model?: string
  kind?: "image" | "video"
  frames?: number
  heard?: boolean
  error?: string
  /** The cut as it was made (fonts, transitions, effects, music), one fact per line; absent when no edit is on record. */
  edit_lines?: string[]
}

export interface Example {
  id: string
  entry_id: string
  industry: string
  shop_type: string
  subtype: string | null
  /** One of the entry's scenes (taxonomy), or null while nobody classified it. */
  scene: string | null
  scene_label: string
  tags: string[]
  rating: number
  status: ExampleStatus
  title: string
  notes: string
  recipe: string
  /** What to learn from the piece, written by a model that looked at it. */
  card: string
  card_state: CardState
  inputs: AssetRef[]
  outputs: AssetRef[]
  source: ExampleSource
  created_by: string | null
  created_at: string | null
  updated_at: string | null
  use_count: number
  /** Times an owner chose this example on the in-app example card. */
  pick_count: number
  industry_label: string
  shop_type_label: string
}

export interface ExampleList {
  items: Example[]
  total: number
  limit: number
  offset: number
}

/** What `POST /prefill` returns and what the form edits before `POST` / `PUT`. */
export interface ExampleDraft {
  entry_id: string | null
  industry: string
  shop_type: string
  subtype: string | null
  scene?: string | null
  title: string
  recipe: string
  inputs: AssetRef[]
  outputs: AssetRef[]
  source: ExampleSource
}

export interface ExampleWrite {
  entry_id: string
  industry: string
  shop_type: string
  subtype: string | null
  scene: string | null
  tags: string[]
  rating: number
  status: ExampleStatus
  title: string
  notes: string
  recipe: string
  /** Sent only when the team changed the text by hand; the server then never reads over it. */
  card?: string
  inputs: { asset_id: string }[]
  outputs: { asset_id: string }[]
  source: ExampleSource
}

export interface ShopType {
  id: string
  label: string
  keywords: string[]
}
export interface Industry {
  id: string
  label: string
  shop_types: ShopType[]
}
export interface Scene {
  id: string
  label: string
}
export interface TaxonomyEntry {
  id: string
  label: string
  /** Empty or absent for an entry the example card does not split by scene (video). */
  scenes?: Scene[]
}
export interface Taxonomy {
  version: string
  entries: TaxonomyEntry[]
  industries: Industry[]
  /** Ids the team may rename but not remove: the App and the first-video form point at them. */
  builtin?: { scenes: Record<string, string[]>; industries: string[]; shop_types: string[] }
}

/** `PUT /taxonomy`: an id is left out for something just added; the server names it. */
export interface TaxonomyWrite {
  scenes: Record<string, { id?: string; label: string }[]>
  industries: { id?: string; label: string; shop_types: { id?: string; label: string; keywords: string[] }[] }[]
}

/** `GET /coverage`: active examples per entry and scene; a cell under `target` cannot fill the card's row. */
export interface CoverageEntry {
  entry_id: string
  label: string
  scenes: { scene: string; label: string; count: number }[]
  unclassified: number
  total: number
}
export interface Coverage {
  target: number
  entries: CoverageEntry[]
}

export interface AssetLink {
  url: string
  mime: string
  name: string
  expires_in: number
}

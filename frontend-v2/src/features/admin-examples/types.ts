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

export interface Example {
  id: string
  entry_id: string
  industry: string
  shop_type: string
  subtype: string | null
  tags: string[]
  rating: number
  status: ExampleStatus
  title: string
  notes: string
  recipe: string
  inputs: AssetRef[]
  outputs: AssetRef[]
  source: ExampleSource
  created_by: string | null
  created_at: string | null
  updated_at: string | null
  use_count: number
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
  tags: string[]
  rating: number
  status: ExampleStatus
  title: string
  notes: string
  recipe: string
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
export interface Taxonomy {
  version: string
  entries: { id: string; label: string }[]
  industries: Industry[]
}

export interface AssetLink {
  url: string
  mime: string
  name: string
  expires_in: number
}

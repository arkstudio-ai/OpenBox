// Resource-centre contracts. The backend classifies `kind` from mime+name in
// one place (api/asset_kinds.py) so the icon, the filter and the preview mode
// all read the same answer.

export const RESOURCE_KINDS = ["image", "video", "audio", "document", "archive", "code", "other"] as const

export type ResourceKind = (typeof RESOURCE_KINDS)[number]

/** Who put the file here: a person, or the agent working in the sandbox. */
export type ResourceSource = "user" | "agent"

export type SourceFilter = "all" | ResourceSource
export type KindFilter = "all" | ResourceKind
export type ResourceSort = "created" | "name" | "size"

/** "all" = every project, "none" = not filed under any project. */
export type ProjectFilter = string

export interface Resource {
  id: string
  name: string
  mime: string
  size: number
  kind: ResourceKind
  source: ResourceSource
  projectId: string | null
  sessionId: string | null
  status: string
  createdAt: string
  /** Where the file lands when a chat message carries it. */
  sandboxPath: string
  /** Presigned GET — expires, so never persist it. */
  url: string
}

export interface ResourcePage {
  items: Resource[]
  total: number
  hasMore: boolean
}

export interface ResourceQuery {
  project: ProjectFilter
  source: SourceFilter
  kind: KindFilter
  q: string
  sort: ResourceSort
  limit?: number
}

export interface ResourceProject {
  id: string
  name: string
}

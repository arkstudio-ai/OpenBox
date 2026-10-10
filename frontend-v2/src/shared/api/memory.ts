import { http, requestBlob } from "@/shared/api/http"

export interface MemoryRecord {
  id: string
  summary: string
  body_available?: boolean
  value?: Record<string, unknown> | string
  type: string
  scope: string
  status: string
  revision: number
  confirmation_status?: string
  confirmation_state?: string
  project_id?: string | null
  created_at?: string
  updated_at?: string
  expires_at?: string | null
  source_count?: number
  index_status?: string
  /** USER_CONFIRMED: the person added, asked for or confirmed it; SYSTEM_VERIFIED: learned from a chat. */
  owner?: string
  /** personal.style.* for how the person likes to be helped. */
  fact_key?: string | null
}

export interface MemorySource {
  id: string
  source_type?: string
  source_kind?: string
  source_id?: string
  source_revision?: string | number
  revision?: string | number
  content?: string | null
  body?: string | null
  body_available?: boolean
  text?: string | null
  content_hash?: string
  hash?: string
  status?: string
  missing_reason?: string | null
  reason_code?: string | null
  created_at?: string
  /** A later correction replaced this source; it no longer backs the memory. */
  superseded?: boolean
  /** For a corrected record: the person's own correcting words. */
  changes?: { body: string; session_id: string | null }[]
  [key: string]: unknown
}

export interface MemoryRevision {
  id?: string
  revision: number
  summary?: string
  value?: Record<string, unknown> | string
  body_available?: boolean
  status?: string
  confirmation_status?: string
  action?: string
  reason?: string
  created_at?: string
  [key: string]: unknown
}

export interface MemoryCleanup {
  status: string
  stopped?: boolean
  tombstone?: Record<string, unknown> | null
  outbox?: Record<string, unknown>[]
  [key: string]: unknown
}

export interface MemoryBundleItem {
  kind: string
  id: string
  revision: number
  text: string
  sources: MemorySource[]
  valid_from?: string | null
  valid_to?: string | null
  confirmation_status?: string
  conflict_status?: string
  score?: number | null
  lexical_score?: number | null
  dense_score?: number | null
  rerank_score?: number | null
}

export interface MemoryBundle {
  request_id: string
  route_attempt_id?: string | null
  scope: Record<string, unknown>
  items: MemoryBundleItem[]
  budget: Record<string, unknown>
  index_generation?: string | null
  lag?: number | Record<string, unknown> | null
  degraded_reasons: string[]
  route?: Record<string, unknown>
  time_context?: Record<string, unknown> | null
}

export type MemoryTab = "active" | "candidate" | "rejected" | "forgotten"

/** Newest first, a page at a time; `query` narrows to memories containing it. */
export function listMemories(
  tab: MemoryTab,
  projectId: string,
  page: { query?: string; offset?: number } = {},
) {
  const params = new URLSearchParams({ limit: "100" })
  if (tab === "rejected") params.set("confirmation_status", "REJECTED")
  else params.set("status", { active: "ACTIVE", candidate: "CANDIDATE", forgotten: "DEPRECATED" }[tab])
  if (projectId) params.set("project_id", projectId)
  if (page.query) params.set("query", page.query)
  if (page.offset) params.set("offset", String(page.offset))
  return http.get<{ memories: MemoryRecord[]; next_offset?: number | null }>(`/api/memories?${params}`)
}

export interface MemorySettings {
  auto_save: boolean
  session_paused: boolean
}

/** Turns still being saved as memories, and ones that could not be. */
export interface MemoryProcessing {
  pending: number
  failed: {
    id: string
    session_id: string
    session_title: string | null
    excerpt: string
    failed_at: string
  }[]
}

const memoryPath = (id: string) => `/api/memories/${encodeURIComponent(id)}`
export const memoryApi = {
  create: (summary: string, projectId: string, requestId: string) =>
    http.post<MemoryRecord>("/api/memories", {
      summary,
      project_id: projectId || null,
      request_id: requestId,
    }),
  confirm: (memory: MemoryRecord, requestId: string) =>
    http.post<MemoryRecord>(`${memoryPath(memory.id)}/confirm`, {
      expected_revision: memory.revision,
      request_id: requestId,
    }),
  reject: (memory: MemoryRecord, requestId: string) =>
    http.post<{ ok: boolean }>(`${memoryPath(memory.id)}/reject`, {
      expected_revision: memory.revision,
      request_id: requestId,
    }),
  correct: (memory: MemoryRecord, summary: string, requestId: string) =>
    http.patch<MemoryRecord>(memoryPath(memory.id), {
      summary,
      expected_revision: memory.revision,
      request_id: requestId,
    }),
  /** Without a revision (a receipt that does not carry one) the server skips the revision check. */
  forget: (memory: Pick<MemoryRecord, "id"> & { revision?: number }, requestId: string, sourceIds?: string[]) =>
    http.post<MemoryCleanup>(`${memoryPath(memory.id)}/forget`, {
      expected_revision: memory.revision,
      request_id: requestId,
      mode: sourceIds ? "sources" : "memory",
      source_ids: sourceIds ?? [],
    }),
  /** The memory as it is now, re-authorized: text only while its sources allow it. */
  get: (id: string) => http.get<MemoryRecord & { body_available: boolean }>(memoryPath(id)),
  history: (id: string) => http.get<{ revisions: MemoryRevision[] }>(`${memoryPath(id)}/history`),
  sources: (id: string) => http.get<{ sources: MemorySource[] }>(`${memoryPath(id)}/sources`),
  cleanup: (id: string) => http.get<MemoryCleanup>(`${memoryPath(id)}/cleanup`),
  /** Automatic saving for the account, and whether one chat is kept out of memory. */
  settings: (sessionId?: string) =>
    http.get<MemorySettings>(
      "/api/memories/settings" + (sessionId ? "?" + new URLSearchParams({ session_id: sessionId }) : ""),
    ),
  setAutoSave: (autoSave: boolean) =>
    http.put<MemorySettings>("/api/memories/settings", { auto_save: autoSave }),
  pauseChat: (sessionId: string, paused: boolean) =>
    http.put<MemorySettings>(`/api/memories/settings/sessions/${encodeURIComponent(sessionId)}`, { paused }),
  /** Everything remembered, as a Markdown file to keep. */
  exportAll: (lang: string) => requestBlob("/api/memories/export?" + new URLSearchParams({ lang })),
  /** Forgets every memory in one project, or everywhere when none is given. */
  forgetAll: (projectId: string) =>
    http.post<{ forgotten: number }>("/api/memories/forget-all", {
      project_id: projectId || null,
      confirm: "forget-all",
    }),
  /** How many current memories rest on one chat; deleting it withdraws them. */
  learnedFrom: (sessionId: string) =>
    http.get<{ count: number }>(`/api/memories/learned-from/${encodeURIComponent(sessionId)}`),
  processing: (projectId: string) =>
    http.get<MemoryProcessing>(
      "/api/memories/processing" + (projectId ? "?" + new URLSearchParams({ project_id: projectId }) : ""),
    ),
  retryTurn: (id: string) =>
    http.post<{ ok: boolean }>(`/api/memories/processing/${encodeURIComponent(id)}/retry`),
  dismissTurn: (id: string) =>
    http.post<{ ok: boolean }>(`/api/memories/processing/${encodeURIComponent(id)}/dismiss`),
  search: (query: string, projectId: string) =>
    http.post<MemoryBundle>("/api/memories/search", {
      query,
      project_id: projectId || null,
      limit: 12,
    }),
}

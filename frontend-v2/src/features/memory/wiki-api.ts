import { http } from "@/shared/api/http"

export interface WikiCandidate {
  id: string
  revision: number
  candidate_hash: string
  title: string
  slug: string
  body: string | null
  body_available: boolean
  status: string
  expected_target_revision: number
  expected_target_hash: string | null
  model: string
  sources: Record<string, unknown>[]
  paragraphs: Record<string, unknown>[]
  usage: Record<string, unknown>
  reason_code?: string | null
  project_id?: string | null
  created_at?: string
}
export interface WikiPage {
  document?: { id: string; filename: string; sections: { id: string; title: string }[]; warnings: string[] }
  id: string
  revision: number
  content_hash: string
  title: string
  slug: string
  body: string | null
  body_available: boolean
  status: string
  sources: Record<string, unknown>[]
  paragraphs: Record<string, unknown>[]
  reason_code?: string | null
  project_id?: string | null
  updated_at?: string
  model?: string
  memory_dependencies?: { id: string; revision: number }[]
  source_details?: WikiSourceDetail[]
  exchange_path?: string
  exchange_links?: Record<string, { page_id?: string; source_id?: string }>
}
export interface WikiSourceDetail {
  filename?: string
  original_pages?: number[]
  edited?: boolean
  id: string
  revision: number
  kind: string
  body: string
  session_id: string | null
  created_at: string
  path?: string | null
  changes?: { body: string; session_id: string | null }[]
}
export interface WikiSummary extends Omit<WikiPage, "body" | "paragraphs" | "sources" | "content_hash"> {
  excerpt: string
  source_count: number
  source_ids: string[]
}
export interface WikiEditSnapshot {
  id: string
  revision: number
  content_hash: string
  title: string
  entries: { id: string; revision: number; text: string; max_length: number }[]
}
export const wikiApi = {
  capabilities: () =>
    http.get<{
      enabled: boolean
      automatic_knowledge: boolean
      model: string
      max_memories: number
      max_sources: number
      max_source_characters: number
      estimated_cost: number | null
    }>("/api/memory-wiki/capabilities"),
  library: (options: { projectId: string; query: string; status: string; offset: number }) => {
    const search = new URLSearchParams({
      query: options.query,
      status: options.status,
      offset: String(options.offset),
    })
    if (options.projectId) search.set("project_id", options.projectId)
    return http.get<{ pages: WikiSummary[]; next_offset: number | null }>(
      "/api/memory-wiki/library?" + search,
    )
  },
  page: (id: string) => http.get<WikiPage>("/api/memory-wiki/pages/" + encodeURIComponent(id)),
  editSnapshot: (id: string) =>
    http.get<WikiEditSnapshot>(`/api/memory-wiki/pages/${encodeURIComponent(id)}/edit`),
  edit: (
    snapshot: WikiEditSnapshot,
    title: string,
    entries: WikiEditSnapshot["entries"],
    requestId: string,
  ) =>
    http.post<{ id: string; status: string }>(
      `/api/memory-wiki/pages/${encodeURIComponent(snapshot.id)}/edit`,
      {
        expected_revision: snapshot.revision,
        content_hash: snapshot.content_hash,
        title,
        entries: entries.map(({ id, revision, text }) => ({ id, revision, text })),
        request_id: requestId,
      },
    ),
}

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
export interface WikiCompileSource {
  id: string
  summary: string
  revision: number
  project_id: string | null
  source_count: number
  source_characters: number
  sources: { id: string; characters: number }[]
}
export interface WikiJob {
  id?: string
  status: string
  candidate_id?: string | null
  page_id?: string
  attempts?: number
  reason_code?: string | null
  usage?: Record<string, unknown>
  model_called?: boolean
}
export interface WikiEditSnapshot {
  id: string
  revision: number
  content_hash: string
  title: string
  entries: { id: string; revision: number; text: string; max_length: number }[]
}
const params = (projectId: string) => (projectId ? `?${new URLSearchParams({ project_id: projectId })}` : "")
const candidateBody = (candidate: WikiCandidate) => ({
  candidate_revision: candidate.revision,
  candidate_hash: candidate.candidate_hash,
  expected_target_revision: candidate.expected_target_revision,
  expected_target_hash: candidate.expected_target_hash,
  request_id: crypto.randomUUID(),
})
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
  compileSources: (projectId: string, offset: number) => {
    const search = new URLSearchParams({ offset: String(offset) })
    if (projectId) search.set("project_id", projectId)
    return http.get<{ memories: WikiCompileSource[]; next_offset: number | null }>(
      "/api/memory-wiki/compile-sources?" + search,
    )
  },
  pages: (projectId: string) => http.get<{ pages: WikiPage[] }>(`/api/memory-wiki/pages${params(projectId)}`),
  candidates: (projectId: string) =>
    http.get<{ candidates: WikiCandidate[] }>(`/api/memory-wiki/candidates${params(projectId)}`),
  compile: (
    slug: string,
    title: string,
    projectId: string,
    options: string | { requestId: string; memoryIds: string[] },
  ) =>
    http.post<WikiJob>("/api/memory-wiki/compile", {
      slug,
      title,
      project_id: projectId || null,
      request_id: typeof options === "string" ? options : options.requestId,
      confirm_cost: true,
      ...(typeof options === "object" ? { memory_ids: options.memoryIds } : {}),
    }),
  job: (id: string) => http.get<WikiJob>(`/api/memory-wiki/jobs/${encodeURIComponent(id)}`),
  approve: (candidate: WikiCandidate) =>
    http.post<WikiPage>(
      `/api/memory-wiki/candidates/${encodeURIComponent(candidate.id)}/approve`,
      candidateBody(candidate),
    ),
  reject: (candidate: WikiCandidate) =>
    http.post<{ status: string }>(
      `/api/memory-wiki/candidates/${encodeURIComponent(candidate.id)}/reject`,
      candidateBody(candidate),
    ),
}

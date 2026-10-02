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
}
export interface WikiPage {
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
    http.get<{ enabled: boolean; model: string; max_memories: number; estimated_cost: number | null }>(
      "/api/memory-wiki/capabilities",
    ),
  pages: (projectId: string) => http.get<{ pages: WikiPage[] }>(`/api/memory-wiki/pages${params(projectId)}`),
  candidates: (projectId: string) =>
    http.get<{ candidates: WikiCandidate[] }>(`/api/memory-wiki/candidates${params(projectId)}`),
  compile: (slug: string, title: string, projectId: string, requestId: string) =>
    http.post<WikiJob>("/api/memory-wiki/compile", {
      slug,
      title,
      project_id: projectId || null,
      request_id: requestId,
      confirm_cost: true,
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

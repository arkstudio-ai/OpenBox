import { http, request, requestBlob } from "@/shared/api/http"

const root = "/api/memory-wiki"
export const wikiParams = (projectId: string, extra: Record<string, string | number> = {}) => {
  const params = new URLSearchParams(Object.entries(extra).map(([key, value]) => [key, String(value)]))
  if (projectId) params.set("project_id", projectId)
  return "?" + params
}
export interface Evidence {
  source_id: string
  revision: number
  quote: string
}
export interface Concept {
  id: string
  revision: number
  title: string | null
  aliases: string[]
  category: string | null
  description: string | null
  page_id: string | null
  page_available?: boolean
  project_id: string | null
  status: string
  memory_ids: string[]
  evidence: Evidence[]
  body_available: boolean
  slug: string
}
export interface Relation {
  id: string
  revision: number
  type: string
  status: string
  from_id: string
  to_id: string
  from_title: string
  to_title: string
  from_page_id: string | null
  to_page_id: string | null
  resolved: boolean
  evidence: Evidence[]
  evidence_hash: string
  project_id: string | null
}
export interface OrganizationPreview {
  input_hash: string
  memory_count: number
  changed_count: number
  reused_count: number
  concept_count: number
  model: string
  max_model_calls: number
  skipped: { memory_id: string; reason_code: string }[]
}
export interface OrganizationRun {
  id: string
  revision: number
  project_id: string | null
  status: string
  phase: string
  memory_count: number
  processed: number
  model_calls: number
  max_model_calls: number
  reused_count: number
  concept_count: number
  reason_code: string | null
  can_resume: boolean
  created_at: string
  pages: {
    concept_id: string
    page_id: string
    job_id?: string
    candidate_id?: string
    status: string
    reason_code?: string
  }[]
  skipped: { memory_id: string; reason_code: string }[]
}
export interface Maintenance {
  revision: number
  enabled: boolean
  compile_pages: boolean
  call_limit: number
  calls_used: number
  window_started_at: string | null
  last_run_id: string | null
  reason_code: string | null
}
export interface ExchangeDocument {
  id: string
  revision: number
  content_hash: string
  path: string
  title: string | null
  slug: string
  type: string
  body: string | null
  frontmatter: Record<string, unknown>
  status: string
  page_id: string | null
  conflict: boolean
}
export interface ExchangeBundle {
  id: string
  revision: number
  project_id: string | null
  documents: ExchangeDocument[]
  warnings: { code: string; path: string; target?: string }[]
}
export const platformApi = {
  concepts: (projectId: string, offset = 0, query = "", category = "") =>
    http.get<{ concepts: Concept[]; next_offset: number | null }>(
      root + "/concepts" + wikiParams(projectId, { offset, query, ...(category ? { category } : {}) }),
    ),
  editConcept: (
    concept: Concept,
    value: { title: string; aliases: string[]; category: string; description: string },
  ) => http.post(root + `/concepts/${concept.id}/edit`, { expected_revision: concept.revision, ...value }),
  merge: (source: Concept, target: Concept) =>
    http.post(root + `/concepts/${source.id}/merge`, {
      source_revision: source.revision,
      target_id: target.id,
      target_revision: target.revision,
    }),
  relations: (projectId: string, offset = 0) =>
    http.get<{ relations: Relation[]; next_offset: number | null }>(
      root + "/relations" + wikiParams(projectId, { offset }),
    ),
  relationDecision: (relation: Relation, action: "approve" | "reject") =>
    http.post(root + `/relations/${relation.id}/decision`, {
      expected_revision: relation.revision,
      evidence_hash: relation.evidence_hash,
      action,
    }),
  preview: (projectId: string) =>
    http.get<OrganizationPreview>(root + "/organization/preview" + wikiParams(projectId)),
  runs: (projectId: string, offset = 0) =>
    http.get<{ runs: OrganizationRun[]; next_offset: number | null }>(
      root + "/organization/runs" + wikiParams(projectId, { offset }),
    ),
  organize: (projectId: string, preview: OrganizationPreview, budget: number, compilePages: boolean) =>
    http.post<OrganizationRun>(root + "/organization/runs", {
      project_id: projectId || null,
      input_hash: preview.input_hash,
      request_id: crypto.randomUUID(),
      max_model_calls: budget,
      compile_pages: compilePages,
      confirm_cost: true,
    }),
  actRun: (run: OrganizationRun, action: "resume" | "cancel", budget?: number) =>
    http.post<OrganizationRun>(root + `/organization/runs/${run.id}/actions`, {
      expected_revision: run.revision,
      action,
      max_model_calls: budget,
      confirm_cost: action === "resume",
    }),
  maintenance: (projectId: string) => http.get<Maintenance>(root + "/maintenance" + wikiParams(projectId)),
  configure: (
    projectId: string,
    previous: Maintenance,
    value: { enabled: boolean; call_limit: number; compile_pages: boolean },
  ) =>
    http.put<Maintenance>(root + "/maintenance", {
      project_id: projectId || null,
      expected_revision: previous.revision,
      ...value,
      confirm_cost: value.enabled,
    }),
  importPreview: (projectId: string, file: File) => {
    const form = new FormData()
    form.append("file", file)
    if (projectId) form.append("project_id", projectId)
    return request<ExchangeBundle>(root + "/exchange/preview", { method: "POST", body: form })
  },
  bundles: (projectId: string, offset = 0) =>
    http.get<{
      bundles: { id: string; created_at: string; project_id: string | null }[]
      next_offset: number | null
    }>(root + "/exchange/bundles" + wikiParams(projectId, { offset })),
  bundle: (id: string) => http.get<ExchangeBundle>(root + `/exchange/bundles/${id}`),
  importDecision: (
    document: ExchangeDocument,
    action: "approve" | "reject" | "rename",
    options?: { slug?: string; acknowledge_warnings?: boolean },
  ) =>
    http.post<ExchangeDocument>(root + `/exchange/documents/${document.id}/decision`, {
      expected_revision: document.revision,
      content_hash: document.content_hash,
      action,
      ...options,
    }),
  export: (projectId: string, format: string) =>
    requestBlob(root + "/exchange/export" + wikiParams(projectId, { format })),
}

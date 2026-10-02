import { http, request, requestBlob } from "@/shared/api/http"

export interface KnowledgeDocument {
  id: string
  filename: string
  revision: number
  status: string
  reason_code: string | null
  chunk_count: number
  indexed_chunks: number
  page_ids: string[]
}

export const documentsApi = {
  list: (projectId: string, offset: number) => {
    const query = new URLSearchParams({ offset: String(offset) })
    if (projectId) query.set("project_id", projectId)
    return http.get<{ documents: KnowledgeDocument[]; next_offset: number | null }>(
      `/api/memory-documents?${query}`,
    )
  },
  upload: (file: File, projectId: string) => {
    const data = new FormData()
    data.append("file", file)
    if (projectId) data.append("project_id", projectId)
    return request<KnowledgeDocument>("/api/memory-documents", { method: "POST", body: data })
  },
  retry: (id: string) =>
    http.post<KnowledgeDocument>(`/api/memory-documents/${encodeURIComponent(id)}/retry`),
  download: async (id: string) => {
    const { blob, filename } = await requestBlob(`/api/memory-documents/${encodeURIComponent(id)}/original`)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement("a")
    anchor.href = url
    anchor.download = filename ?? "document"
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  },
}

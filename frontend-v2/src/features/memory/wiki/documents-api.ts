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
  project_id?: string | null
  bytes?: number
  created_at?: string
  updated_at?: string
}

export interface DocumentUploadResult extends KnowledgeDocument {
  /** False when an upload reuses an existing document, regardless of its processing status. */
  created: boolean
}

export const documentsApi = {
  list: (projectId: string, offset: number) => {
    const query = new URLSearchParams({ offset: String(offset) })
    if (projectId) query.set("project_id", projectId)
    return http.get<{
      documents: KnowledgeDocument[]
      next_offset: number | null
      /** Deleted files whose originals are still being removed from storage. */
      cleanup_pending?: number
    }>(`/api/memory-documents?${query}`)
  },
  upload: (file: File, projectId: string) => {
    const data = new FormData()
    data.append("file", file)
    if (projectId) data.append("project_id", projectId)
    return request<DocumentUploadResult>("/api/memory-documents", { method: "POST", body: data })
  },
  retry: (id: string) =>
    http.post<KnowledgeDocument>(`/api/memory-documents/${encodeURIComponent(id)}/retry`),
  /** Removes the file and everything built from it; chats are untouched.
   *  `original_cleanup` is "pending" while the original is still in storage. */
  remove: (id: string) =>
    http.delete<{ ok: boolean; status: string; original_cleanup?: "done" | "pending" }>(
      `/api/memory-documents/${encodeURIComponent(id)}`,
    ),
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

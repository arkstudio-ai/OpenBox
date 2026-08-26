// Uploading into the resource centre: open a ticket, PUT the bytes straight
// to OSS, then have the backend verify the object landed. The bytes never
// pass through the API — see backend/api/assets.py.
import { http } from "@/shared/api/http"
import { putToStorage } from "@/shared/api/upload"
import type { Resource } from "@/features/resources/types"

interface UploadTicket {
  id: string
  name: string
  sandboxPath: string
  putUrl: string
  headers: Record<string, string>
}

/** Bytes → OSS → verified row. `onProgress` runs for the transfer only. */
export async function uploadResource(
  file: File,
  projectId: string | null,
  onProgress: (fraction: number) => void,
): Promise<Resource> {
  const ticket = await http.post<UploadTicket>("/api/assets", {
    name: file.name,
    mime: file.type || "application/octet-stream",
    size: file.size,
    project_id: projectId ?? undefined,
  })
  await putToStorage(ticket, file, onProgress)
  return http.post<Resource>(`/api/assets/${ticket.id}/complete`)
}

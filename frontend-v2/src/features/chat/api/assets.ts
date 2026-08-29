// OSS asset transfer client. Bytes go browser → OSS on a presigned PUT (the
// backend only signs and records), and the chat trades an asset id for a
// fresh preview URL whenever a card needs one — presigned GETs expire.
import { http } from "@/shared/api/http"

export interface AssetTicket {
  id: string
  name: string
  sandboxPath: string
  putUrl: string
  headers: Record<string, string>
}

export interface AssetInfo {
  id: string
  name: string
  mime: string
  size: number
  sandboxPath: string
  url: string
}

export function createAsset(name: string, mime: string, size: number, sessionId?: string | null) {
  return http.post<AssetTicket>("/api/assets", { name, mime, size, session_id: sessionId ?? undefined })
}

export function completeAsset(id: string) {
  return http.post<AssetInfo>(`/api/assets/${id}/complete`)
}

/** PUT the bytes straight to OSS, reporting progress. XHR because fetch has
 *  no upload progress. The Content-Type header must be exactly what the
 *  backend signed — that is the whole contract. */
export function putToOss(
  ticket: AssetTicket,
  file: File,
  onProgress: (fraction: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("PUT", ticket.putUrl)
    for (const [k, v] of Object.entries(ticket.headers)) xhr.setRequestHeader(k, v)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve()
      else reject(new Error(`OSS PUT ${xhr.status}`))
    }
    xhr.onerror = () => reject(new Error("OSS PUT network error"))
    xhr.send(file)
  })
}

// Moved to shared/api/assets so the jobs feature can surface produced files
// too; re-exported here so chat's existing call sites keep their import.
export { useAssetUrl } from "@/shared/api/assets"

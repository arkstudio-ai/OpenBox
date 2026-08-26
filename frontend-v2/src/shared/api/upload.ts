// Direct-to-storage upload transport. No business semantics: it PUTs a File
// to a presigned URL and reports progress. XHR rather than fetch because
// fetch still has no upload-progress event.
//
// The Content-Type header must be exactly the one the URL was signed with —
// OSS folds it into the signature, so a mismatch is a 403, not a warning.

export interface PresignedPut {
  putUrl: string
  headers: Record<string, string>
}

export function putToStorage(
  ticket: PresignedPut,
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
      else reject(new Error(`storage PUT ${xhr.status}`))
    }
    xhr.onerror = () => reject(new Error("storage PUT network error"))
    xhr.send(file)
  })
}

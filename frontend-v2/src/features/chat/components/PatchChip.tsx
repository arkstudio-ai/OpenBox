import { useState } from "react"
import { Download, LoaderCircle } from "lucide-react"
import { DiffPreview } from "./DiffPreview"
import { http } from "@/shared/api/http"
import type { FilePart, PatchPart } from "@/shared/types/api"

interface PatchChipProps {
  part: PatchPart
  sessionId: string
}

/** Changed-files list for one step. The counts ride along on the patch part
 *  itself, so this needs no diff request — the panel fetches the real hunks
 *  once, when the user opens one. */
export function PatchChip({ part, sessionId }: PatchChipProps) {
  return (
    <div className="flex flex-col">
      {part.files.map((f) => (
        <DiffPreview key={f.path} file={f} sessionId={sessionId} />
      ))}
    </div>
  )
}

/** A produced-file reference (file part) — a lightweight mono chip. */
export function FileChip({ part }: { part: FilePart }) {
  const name = part.path.split("/").pop() ?? part.path
  const [downloading, setDownloading] = useState(false)

  async function download() {
    if (!part.asset_id || downloading) return
    setDownloading(true)
    try {
      const { url } = await http.get<{ url: string }>(
        `/api/assets/${encodeURIComponent(part.asset_id)}/url?download=true`,
      )
      const link = document.createElement("a")
      link.href = url
      link.download = name
      link.rel = "noreferrer"
      document.body.appendChild(link)
      link.click()
      link.remove()
    } finally {
      setDownloading(false)
    }
  }

  const className = "border-hair flex items-center gap-2.5 self-start rounded-full border px-4 py-2"
  if (part.asset_id) {
    return (
      <button type="button" onClick={() => void download()} disabled={downloading} className={className}>
        <span className="bg-s400 size-2 rounded-full" />
        <span className="text-ink font-mono text-sm">{name}</span>
        {downloading ? (
          <LoaderCircle className="text-n600 size-3.5 animate-spin" />
        ) : (
          <Download className="text-n600 size-3.5" />
        )}
      </button>
    )
  }
  return (
    <div className={className}>
      <span className="bg-s400 size-2 rounded-full" />
      <span className="text-ink font-mono text-sm">{name}</span>
    </div>
  )
}

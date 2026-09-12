import { useTranslation } from "react-i18next"
import { Download } from "lucide-react"
import { formatBytes } from "@/shared/lib/format"
import { Spinner } from "@/shared/ui/Spinner"
import { StaleAccessError } from "../../api/access"
import { usePayload } from "../../api/queries"
import { useDownloadPayload } from "../../hooks/useDownloadPayload"
import type { PayloadRef } from "../../types/protocol"
import { AvailabilityNote } from "./AvailabilityNote"
import { useInspector } from "./context"
import { JsonTree } from "./JsonTree"
import { mediaKindOf } from "./media"
import { MediaElement } from "./MediaElement"
import { TextBlock } from "./TextBlock"
import { NS } from "./types"

interface PayloadViewProps {
  reference: PayloadRef
  /** Captured name offered for the explicit download; the payload id otherwise. */
  filename?: string | null
}

function parseJson(text: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch {
    return { ok: false }
  }
}

/**
 * Protected content read through the admin payload endpoint at the inspector's
 * watermark — never a public URL, the owner's attachment API or a sandbox path.
 * The query resolves "not produced yet at this position", "deleted" (even when
 * replaying an earlier position) and "corrupt" as states without a body and
 * revalidates, so a later deletion unmounts the media element (releasing its
 * object URL) together with the download action. Downloading is an explicit,
 * fresh, server-checked read; the Blob on screen is never handed out.
 */
export function PayloadView({ reference, filename }: PayloadViewProps) {
  const { t } = useTranslation(NS)
  const { sessionId, throughSeq } = useInspector()
  const payload = usePayload(sessionId, throughSeq, reference.payload_id)
  const download = useDownloadPayload(sessionId, throughSeq, reference.payload_id)
  const content = payload.data

  if (content && content.availability !== "available")
    return <AvailabilityNote state={content.availability} />
  if (payload.error) {
    return (
      <span role="alert" className="text-dangerink text-xs">
        {t("payload.failed")}
      </span>
    )
  }
  if (!content) {
    return (
      <span className="text-n500 inline-flex items-center gap-2 text-xs">
        <Spinner className="size-3.5" />
        {t("payload.loading")}
      </span>
    )
  }
  const kind = mediaKindOf(content.mediaType)
  const json =
    content.text !== null && content.mediaType.includes("json")
      ? parseJson(content.text)
      : { ok: false as const }
  const downloadFailed = !!download.error && !(download.error instanceof StaleAccessError)
  return (
    <div className="flex flex-col gap-2" data-testid="trajectory-payload">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-n500 text-2xs">
          {t("payload.meta", { type: content.mediaType, size: formatBytes(content.size) })}
        </span>
        <button
          type="button"
          className="border-hair hover:bg-hairsoft inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs disabled:opacity-40"
          onClick={() => download.mutate({ filename: filename ?? null })}
          disabled={download.isPending}
          data-testid="trajectory-payload-download"
        >
          <Download size={12} aria-hidden />
          {t("payload.download")}
        </button>
        {downloadFailed && (
          <span role="alert" className="text-dangerink text-xs">
            {t("payload.failed")}
          </span>
        )}
      </div>
      {kind && (
        <MediaElement
          blob={content.blob}
          kind={kind}
          label={t("payload.mediaLabel", { id: reference.payload_id })}
        />
      )}
      {json.ok && <JsonTree value={json.value} />}
      {!json.ok && content.text !== null && <TextBlock text={content.text} />}
    </div>
  )
}

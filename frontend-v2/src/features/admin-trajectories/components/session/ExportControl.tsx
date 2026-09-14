import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Download, PackageOpen } from "lucide-react"
import { StaleAccessError } from "../../api/access"
import { useCreateExport, useExportDownload, useExportJob } from "../../api/queries"
import { EXPORT_STATUS_LABELS, labelKey } from "../../constants/labels"
import type { Seq } from "../../types/protocol"

interface ExportControlProps {
  sessionId: string
  /** The shown position; the package is fixed there. */
  throughSeq: Seq | null
  enabled: boolean
}

const BUTTON =
  "border-hair hover:bg-hairsoft inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs disabled:opacity-40"

function visibleError(error: unknown): boolean {
  return !!error && !(error instanceof StaleAccessError)
}

/**
 * The only write this viewer can make: ask the server to package the
 * recording up to the shown position, then download it through the protected
 * API. It never changes the watched session.
 */
export function ExportControl({ sessionId, throughSeq, enabled }: ExportControlProps) {
  const { t } = useTranslation("admin-trajectories")
  const create = useCreateExport(sessionId)
  const download = useExportDownload(sessionId)
  const [exportId, setExportId] = useState<string | null>(null)
  const job = useExportJob(sessionId, exportId)
  const status = job.data?.status ?? (exportId ? "pending" : null)

  const start = () => {
    if (!throughSeq) return
    create.mutate(throughSeq, { onSuccess: (created) => setExportId(created.export_id) })
  }

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="trajectory-export">
      <button
        type="button"
        className={BUTTON}
        onClick={start}
        disabled={!enabled || !throughSeq || create.isPending}
      >
        <PackageOpen size={13} aria-hidden />
        {throughSeq ? t("export.create", { seq: throughSeq }) : t("export.createDisabled")}
      </button>
      {status && (
        <span role="status" className="text-n600 text-xs" data-export-status={status}>
          {t(labelKey(EXPORT_STATUS_LABELS, status, "export.status.other"), {
            value: status,
            seq: job.data?.through_seq ?? "",
          })}
        </span>
      )}
      {status === "completed" && exportId && (
        <button
          type="button"
          className={BUTTON}
          onClick={() => download.mutate(exportId)}
          disabled={download.isPending}
        >
          <Download size={13} aria-hidden />
          {t("export.download")}
        </button>
      )}
      {(visibleError(create.error) || visibleError(download.error) || job.data?.error) && (
        <span role="alert" className="text-dangerink text-xs">
          {job.data?.error ? t("export.failedReason", { reason: job.data.error }) : t("export.failed")}
        </span>
      )}
      {!enabled && <span className="text-n500 text-2xs">{t("export.unavailable")}</span>}
    </div>
  )
}

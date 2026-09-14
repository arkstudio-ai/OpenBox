import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { AvailabilityNote } from "./AvailabilityNote"
import { NS, type InspectedRecord } from "./types"
import { useResolvedRecord } from "./useResolvedRecord"

interface ResolvedRecordProps {
  record: InspectedRecord
  /** Read only these `data` fields' references — the content a panel shows. The whole record otherwise. */
  fields?: readonly string[]
  children: (record: InspectedRecord) => ReactNode
}

/**
 * Renders `children` with the record's `$ref` values read, exactly as the
 * server's fully expanded record would render (SPEC §11.2). Until then it
 * says what is missing: loading, content that is not available, or a failed
 * read with a retry. A record without references renders at once.
 */
export function ResolvedRecord({ record, fields, children }: ResolvedRecordProps) {
  const { t } = useTranslation(NS)
  const { resolution, retry } = useResolvedRecord(record, fields)
  switch (resolution.status) {
    case "ready":
      return children(resolution.value)
    case "unavailable":
      return <AvailabilityNote state={resolution.availability} />
    case "error":
      return (
        <div
          role="alert"
          className="text-dangerink flex items-center gap-2 text-xs"
          data-testid="trajectory-content-failed"
        >
          {t("content.failed")}
          <button type="button" className="underline" onClick={retry}>
            {t("common.retry")}
          </button>
        </div>
      )
    default:
      return (
        <span
          className="text-n500 inline-flex items-center gap-2 text-xs"
          data-testid="trajectory-content-loading"
        >
          <Spinner className="size-3.5" />
          {t("content.loading")}
        </span>
      )
  }
}

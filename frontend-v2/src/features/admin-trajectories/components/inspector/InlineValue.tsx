import { useTranslation } from "react-i18next"
import { isPayloadEnvelope } from "../../types/protocol"
import { AvailabilityNote } from "./AvailabilityNote"
import { JsonTree } from "./JsonTree"
import { PayloadView } from "./PayloadView"
import { RecordLink } from "./RecordLink"
import { TextBlock } from "./TextBlock"
import { NS } from "./types"

interface InlineValueProps {
  value: unknown
  /** Treat a string value as the id of a record of this kind, e.g. "request". */
  linkPrefix?: string
}

const SHORT = 160

/** A compact rendering for label/value grids; long or structured values fall back to the full viewers. */
export function InlineValue({ value, linkPrefix }: InlineValueProps) {
  const { t } = useTranslation(NS)
  if (isPayloadEnvelope(value)) {
    const availability = value.$payload.availability ?? "available"
    if (availability === "deleted")
      return <AvailabilityNote state="deleted" reason={value.$payload.reason ?? null} />
    if (availability === "corrupt" || availability === "unsupported")
      return <AvailabilityNote state={availability} />
    return <PayloadView reference={value.$payload} />
  }
  if (value === null || value === "") return <AvailabilityNote state="empty" />
  if (typeof value === "string") {
    if (linkPrefix) return <RecordLink recordId={`${linkPrefix}:${value}`} label={value} />
    if (value.length <= SHORT && !value.includes("\n")) return <span className="break-words">{value}</span>
    return <TextBlock text={value} />
  }
  if (typeof value === "boolean") return <span>{value ? t("common.yes") : t("common.no")}</span>
  if (typeof value === "number") return <span className="font-mono">{String(value)}</span>
  return <JsonTree value={value} openDepth={1} />
}

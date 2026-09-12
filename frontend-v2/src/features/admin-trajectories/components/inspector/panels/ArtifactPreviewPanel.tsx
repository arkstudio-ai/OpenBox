import { formatBytes } from "@/shared/lib/format"
import type { PayloadRef } from "../../../types/protocol"
import { fileDiffOf } from "../../../utils/artifact"
import { classifyValue } from "../../../utils/availability"
import { isPlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { PayloadView } from "../PayloadView"
import { TextBlock } from "../TextBlock"
import { UnifiedDiffView } from "../UnifiedDiffView"
import type { PanelProps } from "../types"
import { ValueView } from "../ValueView"

function payloadRef(value: unknown): PayloadRef | null {
  return isPlainObject(value) && typeof value.payload_id === "string"
    ? (value as unknown as PayloadRef)
    : null
}

/**
 * The artifact as retained at this position: a file change as its captured
 * diff (or the retained new version when no diff could be made), media through
 * the protected payload endpoint. Deleted or unretained content says so — a
 * current file with the same name is never shown instead.
 */
export function ArtifactPreviewPanel({ record }: PanelProps) {
  const data = record.data ?? {}
  const file = fileDiffOf(record)
  const meta = [
    file?.path ?? data.name ?? data.path,
    data.media_type,
    typeof data.size_bytes === "number" ? formatBytes(data.size_bytes) : null,
  ]
    .filter((value): value is string => typeof value === "string" && !!value)
    .join(" · ")
  const overall = classifyValue(record, "availability" in data, {
    availability: data.availability ?? "available",
  })
  let body
  if (record.status === "deleted") {
    body = <AvailabilityNote state="deleted" reason={typeof data.reason === "string" ? data.reason : null} />
  } else if (overall.state !== "available") {
    body = <ValueView field={overall} />
  } else if (file && file.diff.state === "available" && typeof file.diff.value === "string") {
    body = <UnifiedDiffView diff={file.diff.value} />
  } else if (file && file.after.field.state === "available" && file.after.text !== null) {
    body = <TextBlock text={file.after.text} />
  } else if (file) {
    body = <ValueView field={file.after.field.state === "available" ? file.diff : file.after.field} />
  } else {
    const reference = payloadRef(data.payload)
    body = reference ? (
      <PayloadView reference={reference} filename={typeof data.name === "string" ? data.name : null} />
    ) : (
      <AvailabilityNote state="not_recorded" />
    )
  }
  return (
    <div className="flex flex-col gap-2" data-testid="trajectory-artifact-preview">
      {meta && <p className="text-n600 font-mono text-xs break-all">{meta}</p>}
      {body}
    </div>
  )
}

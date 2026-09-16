import { useTranslation } from "react-i18next"
import { formatBytes } from "@/shared/lib/format"
import type { TrajectoryEvent } from "../../types/protocol"
import { outputTruncation } from "../../utils/toolOutput"
import { NS } from "./types"

interface OutputTruncationNoteProps {
  events: readonly TrajectoryEvent[] | undefined
}

/** One line saying the recorded tool output is not the whole output: its full size and sha256. */
export function OutputTruncationNote({ events }: OutputTruncationNoteProps) {
  const { t } = useTranslation(NS)
  const truncation = outputTruncation(events)
  if (!truncation) return null
  return (
    <p className="text-n600 text-xs break-all" data-testid="trajectory-output-truncated">
      {truncation.kind === "final"
        ? t("tool.outputTruncated", {
            size: truncation.bytes !== null ? formatBytes(truncation.bytes) : t("common.dash"),
            sha256: truncation.sha256 ?? t("common.dash"),
          })
        : t("tool.streamTruncated")}
    </p>
  )
}

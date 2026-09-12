import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import type { Seq } from "../../types/protocol"
import { SummaryPreview } from "./SummaryPreview"

interface PendingPositionProps {
  sessionId: string
  headSeq: Seq
  /** The requested replay position, or null while following live. */
  pinnedSeq: Seq | null
}

/**
 * What stands in for the workspace until its position is ready. Following
 * live, server summaries at the head give a fast first paint. At a pinned
 * replay position only a neutral placeholder is shown: head summaries would
 * reveal titles, statuses and durations from after that position.
 */
export function PendingPosition({ sessionId, headSeq, pinnedSeq }: PendingPositionProps) {
  const { t } = useTranslation("admin-trajectories")
  if (pinnedSeq === null) return <SummaryPreview sessionId={sessionId} headSeq={headSeq} />
  return (
    <div
      className="border-hair bg-card text-n600 flex items-center justify-center gap-2 rounded-xl border py-16 text-sm"
      data-testid="trajectory-position-loading"
      data-seq={pinnedSeq}
    >
      <Spinner className="size-4" />
      {t("session.positionLoading")}
    </div>
  )
}

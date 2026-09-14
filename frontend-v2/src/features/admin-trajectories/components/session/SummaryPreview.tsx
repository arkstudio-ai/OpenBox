import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { useRecordPages } from "../../api/queries"
import { useStableWatermark } from "../../hooks/useStableWatermark"
import { useTrajectoryView } from "../../stores/view"
import type { Seq } from "../../types/protocol"
import { buildTree, flattenTree } from "../../utils/view"
import { buildOrdinals } from "./ordinals"
import { RecordTable } from "./RecordTable"
import type { RowContext } from "./RecordRowView"

interface SummaryPreviewProps {
  sessionId: string
  headSeq: Seq
}

const NONE = new Map<string, string>()
const noop = () => undefined

/**
 * First paint for a large session: server record summaries at one pinned
 * watermark while the full replay loads. Summaries carry no captured content,
 * so nothing here can be inspected until the position is ready.
 */
export function SummaryPreview({ sessionId, headSeq }: SummaryPreviewProps) {
  const { t, i18n } = useTranslation("admin-trajectories")
  const pinned = useStableWatermark(headSeq, false)
  const pages = useRecordPages(sessionId, pinned, {}, true)
  const timeMode = useTrajectoryView((s) => s.timeMode)
  const records = useMemo(() => pages.data?.pages.flatMap((page) => page.items) ?? [], [pages.data])
  const tree = useMemo(() => buildTree(records), [records])
  const rows = useMemo(() => flattenTree(tree, {}), [tree])
  const context = useMemo<RowContext>(
    () => ({
      ordinals: buildOrdinals(tree.nodes.values()),
      clock: null,
      timeMode,
      locale: i18n.language,
      agentNames: NONE,
    }),
    [i18n.language, timeMode, tree],
  )
  return (
    <section
      className="border-hair bg-card flex h-[28rem] flex-col overflow-hidden rounded-xl border"
      data-testid="trajectory-summary-preview"
    >
      <div className="border-hair text-n600 flex items-center gap-2 border-b px-3 py-2 text-xs">
        <Spinner className="size-3.5" />
        <span className="min-w-0 flex-1">{t("preview.loading", { seq: pinned ?? headSeq })}</span>
        {pages.hasNextPage && (
          <button
            type="button"
            className="text-a700 hover:underline"
            onClick={() => void pages.fetchNextPage()}
            disabled={pages.isFetchingNextPage}
          >
            {t("preview.older")}
          </button>
        )}
      </div>
      {pages.error ? (
        <p role="alert" className="text-dangerink p-4 text-xs">
          {t("preview.failed")}
        </p>
      ) : (
        <RecordTable rows={rows} selectedId={null} onSelect={noop} onToggle={noop} context={context} />
      )}
    </section>
  )
}

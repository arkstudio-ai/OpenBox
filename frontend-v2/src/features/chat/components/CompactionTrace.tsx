import { lazy, Suspense } from "react"
import { useTranslation } from "react-i18next"
import type { CompactionView } from "../lib/compaction-view"
import { TraceShell } from "./TraceShell"

const Markdown = lazy(() => import("./Markdown"))

/** Compaction is a stream stage, not the assistant's answer. */
export function CompactionTrace({ item }: { item: CompactionView }) {
  const { t } = useTranslation("chat")
  const running = item.status === "running"
  return (
    <div data-compaction-state={item.status}>
      <TraceShell
        title={t("trace.compaction.title")}
        subtitle={t(`trace.compaction.${item.status}`)}
        streaming={running}
        unmountOnClose
      >
        <div className="text-n700 border-hair max-h-80 overflow-y-auto border-s ps-3 text-sm leading-6 [overflow-wrap:anywhere]">
          <p className="text-n600 mb-2 text-xs">{t(`trace.compaction.${item.status}`)}</p>
          {item.summary ? (
            <Suspense fallback={<p className="whitespace-pre-wrap">{item.summary}</p>}>
              <Markdown text={item.summary} streaming={running} />
            </Suspense>
          ) : (
            <p className="text-n600 text-xs">{t("trace.compaction.noSummary")}</p>
          )}
        </div>
      </TraceShell>
    </div>
  )
}

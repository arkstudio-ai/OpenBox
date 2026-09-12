import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { formatNumber, formatTokens } from "@/shared/lib/format"
import type { TrajectoryStatistics } from "../../types/protocol"
import { formatDuration } from "../../utils/time"
import { AvailabilityNote } from "../inspector/AvailabilityNote"

interface StatisticsStripProps {
  statistics: TrajectoryStatistics
}

interface StatProps {
  label: string
  children: ReactNode
  testId: string
}

function Stat({ label, children, testId }: StatProps) {
  return (
    <div className="flex min-w-0 items-baseline gap-1.5 whitespace-nowrap" data-testid={testId}>
      <dt className="text-n500 text-2xs">{label}</dt>
      <dd className="text-ink font-mono text-sm">{children}</dd>
    </div>
  )
}

/**
 * Totals of the recorded range at the shown position, folded from the same
 * projection as the table — never the live header's numbers during replay.
 * One compact line, so the records below keep the screen.
 */
export function StatisticsStrip({ statistics }: StatisticsStripProps) {
  const { t, i18n } = useTranslation("admin-trajectories")
  const duration = formatDuration(statistics.duration_ms, i18n.language)
  return (
    <section
      aria-label={t("stats.title")}
      className="border-hair bg-card rounded-xl border px-4 py-2"
      data-testid="trajectory-statistics"
    >
      <dl className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
        <Stat label={t("stats.requests")} testId="trajectory-stat-requests">
          {formatNumber(statistics.request_count)}
        </Stat>
        <Stat label={t("stats.tools")} testId="trajectory-stat-tools">
          {formatNumber(statistics.tool_count)}
        </Stat>
        <Stat label={t("stats.errors")} testId="trajectory-stat-errors">
          {formatNumber(statistics.error_count)}
        </Stat>
        <Stat label={t("stats.unknown")} testId="trajectory-stat-unknown">
          {formatNumber(statistics.unknown_count)}
        </Stat>
        <Stat label={t("stats.inputTokens")} testId="trajectory-stat-input">
          {statistics.input_tokens === null ? (
            <AvailabilityNote state="not_recorded" />
          ) : (
            formatTokens(statistics.input_tokens)
          )}
        </Stat>
        <Stat label={t("stats.outputTokens")} testId="trajectory-stat-output">
          {statistics.output_tokens === null ? (
            <AvailabilityNote state="not_recorded" />
          ) : (
            formatTokens(statistics.output_tokens)
          )}
        </Stat>
        <Stat label={t("stats.duration")} testId="trajectory-stat-duration">
          {duration ?? <AvailabilityNote state="not_recorded" />}
        </Stat>
        <div className="text-n600 text-2xs min-w-0" data-testid="trajectory-stat-scope">
          <dt className="sr-only">{t("stats.scope")}</dt>
          <dd>
            {t(statistics.usage_complete ? "stats.scopeComplete" : "stats.scopePartial", {
              seq: statistics.through_seq,
            })}
          </dd>
        </div>
      </dl>
    </section>
  )
}

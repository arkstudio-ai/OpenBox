// Execution history for one job, one row per run; picking a row is how the
// task page decides which transcript to show.
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"
import { formatDuration, formatRelative, formatTokens } from "@/shared/lib/format"
import { useCronRuns } from "@/features/cron/api/cron"
import { RUN_STATUS_KEYS } from "@/features/cron/constants"
import { isSilentResult } from "@/features/cron/utils/schedule"
import type { CronRun } from "@/features/cron/types"

function StatusChip({ status }: { status: CronRun["status"] }) {
  const { t } = useTranslation("cron")
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-xs",
        status === "ok" && "bg-n300 text-n800",
        status === "error" && "bg-dangersoft text-danger",
        (status === "running" || status === "skipped") && "bg-hairsoft text-n600",
      )}
    >
      {t(RUN_STATUS_KEYS[status] ?? RUN_STATUS_KEYS.skipped)}
    </span>
  )
}

function RunRow({
  run,
  selected,
  onSelect,
}: {
  run: CronRun
  selected: boolean
  onSelect: (run: CronRun) => void
}) {
  const { t } = useTranslation("cron")
  const silent = run.status === "ok" && isSilentResult(run.summary_text)
  return (
    <button
      type="button"
      aria-current={selected ? "true" : undefined}
      onClick={() => onSelect(run)}
      className={cn(
        "flex w-full flex-col gap-1 rounded-xl px-3 py-2.5 text-start",
        selected ? "bg-n200" : "hover:bg-hairsoft",
      )}
    >
      <span className="text-n600 flex flex-wrap items-center gap-2 text-xs">
        <StatusChip status={run.status} />
        {silent && <span className="bg-hairsoft rounded-full px-2 py-0.5">{t("run.silent")}</span>}
        {run.started_at && <span>{formatRelative(run.started_at)}</span>}
        <span>{formatDuration(run.duration_ms / 1000)}</span>
        {run.total_tokens > 0 && (
          <span>
            {t("run.tokens", { count: run.total_tokens, formatted: formatTokens(run.total_tokens) })}
          </span>
        )}
      </span>
      {run.status === "error" ? (
        <span className="text-danger text-xs text-pretty">{t("run.failed")}</span>
      ) : (
        !silent &&
        run.summary_text && (
          <span className="text-n700 line-clamp-2 text-xs text-pretty">{run.summary_text}</span>
        )
      )}
    </button>
  )
}

interface Props {
  jobId: string
  selectedRunId: string | null
  onSelect: (run: CronRun) => void
  /** Poll while the job is running, so the row in progress keeps up. */
  live?: boolean
}

export function CronRunList({ jobId, selectedRunId, onSelect, live = false }: Props) {
  const { t } = useTranslation("cron")
  const runs = useCronRuns(jobId, true, live)

  if (runs.isPending) {
    return (
      <div className="text-n600 flex items-center gap-2 px-3 py-3 text-xs">
        <Spinner />
        <span>{t("run.loading")}</span>
      </div>
    )
  }
  if (runs.isError) {
    return <span className="text-danger block px-3 py-3 text-xs">{t("run.loadFailed")}</span>
  }
  if (!runs.data || runs.data.length === 0) {
    return <span className="text-n600 block px-3 py-3 text-xs">{t("run.empty")}</span>
  }
  return (
    <div className="flex flex-col gap-0.5">
      {runs.data.map((run) => (
        <RunRow key={run.id} run={run} selected={run.id === selectedRunId} onSelect={onSelect} />
      ))}
      <span className="text-n500 px-3 pt-2 text-[11px]">{t("run.retentionHint")}</span>
    </div>
  )
}

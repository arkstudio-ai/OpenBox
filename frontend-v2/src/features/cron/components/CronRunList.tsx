import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
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

function RunRow({ run }: { run: CronRun }) {
  const { t } = useTranslation("cron")
  const silent = run.status === "ok" && isSilentResult(run.summary_text)
  return (
    <div className="flex flex-col gap-1 border-t border-hairsoft py-2.5 first:border-t-0">
      <div className="flex flex-wrap items-center gap-2 text-xs text-n600">
        <StatusChip status={run.status} />
        {silent && <span className="rounded-full bg-hairsoft px-2 py-0.5">{t("run.silent")}</span>}
        {run.started_at && <span>{formatRelative(run.started_at)}</span>}
        <span>{formatDuration(run.duration_ms / 1000)}</span>
        {run.total_tokens > 0 && (
          <span>{t("run.tokens", { count: run.total_tokens, formatted: formatTokens(run.total_tokens) })}</span>
        )}
        {run.temp_session_id && (
          <Link to={paths.chat(run.temp_session_id)} className="text-ink underline-offset-2 hover:underline">
            {t("run.transcript")}
          </Link>
        )}
      </div>
      {run.status === "error" ? (
        <span className="text-pretty text-xs text-danger">{t("run.failed")}</span>
      ) : (
        !silent &&
        run.summary_text && (
          <span className="line-clamp-2 text-pretty text-xs text-n700">{run.summary_text}</span>
        )
      )}
    </div>
  )
}

/** Execution history for one job; loaded only while expanded. */
export function CronRunList({ jobId, open }: { jobId: string; open: boolean }) {
  const { t } = useTranslation("cron")
  const runs = useCronRuns(jobId, open)

  if (!open) return null
  if (runs.isPending) {
    return (
      <div className="flex items-center gap-2 border-t border-hairsoft py-3 text-xs text-n600">
        <Spinner />
        <span>{t("run.loading")}</span>
      </div>
    )
  }
  if (runs.isError) {
    return <span className="border-t border-hairsoft py-3 text-xs text-danger">{t("run.loadFailed")}</span>
  }
  if (!runs.data || runs.data.length === 0) {
    return <span className="border-t border-hairsoft py-3 text-xs text-n600">{t("run.empty")}</span>
  }
  return (
    <div className="flex flex-col">
      {runs.data.map((run) => (
        <RunRow key={run.id} run={run} />
      ))}
      <span className="pt-1 text-[11px] text-n500">{t("run.retentionHint")}</span>
    </div>
  )
}

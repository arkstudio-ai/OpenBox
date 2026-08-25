import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { Tooltip } from "@/shared/ui/Tooltip"
import { emitAppEvent } from "@/shared/events/bus"
import { formatRelative } from "@/shared/lib/format"
import { useCronJobs } from "@/features/cron/api/cron"
import { useCronLiveEvents } from "@/features/cron/hooks/useCronLiveEvents"
import { useCurrentProjectId } from "@/features/cron/hooks/useCurrentProjectId"
import type { CronJob } from "@/features/cron/types"

/** Aggregate view of one session's jobs for the pill. Exported for tests. */
export function summarize(jobs: CronJob[]) {
  const running = jobs.some((j) => j.running)
  const failed = jobs.filter((j) => j.last_status === "error")
  const enabled = jobs.filter((j) => j.enabled)
  const nextRun = enabled
    .map((j) => j.next_run_at)
    .filter((v): v is string => Boolean(v))
    .sort()[0]
  const lastRun = jobs
    .map((j) => j.last_run_at)
    .filter((v): v is string => Boolean(v))
    .sort()
    .at(-1)
  const autoDisabled = jobs.some((j) => (j.last_error ?? "").startsWith("[auto-disabled"))
  return { running, failedCount: failed.length, nextRun, lastRun, autoDisabled }
}

/** Topbar pill: last-run state dot + time to next run. Hidden when the session
 *  has no scheduled tasks. Click opens the workbench cron tab. */
export function CronStatusPill({ sessionId }: { sessionId: string | null }) {
  const { t } = useTranslation("cron")
  useCronLiveEvents()
  const jobs = useCronJobs()
  const projectId = useCurrentProjectId(sessionId)

  if (!sessionId || !projectId) return null
  const mine = (jobs.data ?? []).filter((j) => j.project_id === projectId)
  if (mine.length === 0) return null

  const s = summarize(mine)
  const dotCls = s.running
    ? "bg-accent animate-pulse"
    : s.failedCount > 0 || s.autoDisabled
      ? "bg-danger"
      : mine.some((j) => j.enabled)
        ? "bg-sage"
        : "bg-n400"

  const label = s.running
    ? t("pill.running")
    : s.nextRun
      ? formatRelative(s.nextRun)
      : t("pill.paused")

  const tooltip = [
    s.lastRun
      ? s.failedCount > 0
        ? t("pill.lastFailed", { when: formatRelative(s.lastRun) })
        : t("pill.lastOk", { when: formatRelative(s.lastRun) })
      : t("pill.neverRan"),
    s.nextRun ? t("pill.next", { when: formatRelative(s.nextRun) }) : "",
  ]
    .filter(Boolean)
    .join("\n")

  return (
    <Tooltip label={tooltip} side="bottom">
      <button
        type="button"
        onClick={() => emitAppEvent("workbench.open", { kind: "cron" })}
        aria-label={t("pill.aria")}
        className={cn(
          "flex h-8 flex-none items-center gap-1.5 rounded-full border border-hair px-2.5",
          "text-xs text-n700 hover:bg-hairsoft",
        )}
      >
        <span className={cn("size-1.5 flex-none rounded-full", dotCls)} />
        <span className="max-w-24 truncate">{label}</span>
        {s.failedCount > 0 && (
          <span className="rounded-full bg-dangersoft px-1.5 text-[10px] text-dangerink">
            {s.failedCount}
          </span>
        )}
      </button>
    </Tooltip>
  )
}

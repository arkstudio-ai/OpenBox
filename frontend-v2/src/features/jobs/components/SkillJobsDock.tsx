import { useTranslation } from "react-i18next"
import { useSessionSkillJobs } from "@/features/jobs/api/jobs"
import { useSkillJobLiveEvents } from "@/features/jobs/hooks/useSkillJobLiveEvents"
import { TERMINAL_JOB_STATUSES, type SkillJobSnapshot } from "@/features/jobs/types"
import { SkillJobCard } from "./SkillJobCard"

/** Active jobs first; only recently finished terminal jobs stay visible.
 *  Exported for tests. */
/** What the dock should show, given what the transcript already shows.
 *
 *  A finished job writes a receipt into the transcript immediately, and that
 *  receipt carries the same status and the same video. Keeping its live card
 *  as well showed the identical result twice and read as two separate pieces
 *  of work — so a job drops out of the dock the moment its receipt lands.
 *
 *  Terminal jobs with no receipt yet (the write is a moment behind, or chat
 *  receipts are switched off) stay briefly, so a result is never nowhere.
 */
export function visibleJobs(
  jobs: SkillJobSnapshot[],
  now = Date.now(),
  receiptedJobIds: ReadonlySet<string> = new Set(),
): SkillJobSnapshot[] {
  const active = jobs.filter((j) => !TERMINAL_JOB_STATUSES.has(j.status))
  const recentTerminal = jobs
    .filter((j) => TERMINAL_JOB_STATUSES.has(j.status))
    .filter((j) => !receiptedJobIds.has(j.jobId))
    .filter((j) => {
      const completed = j.completedAt ? Date.parse(j.completedAt) : NaN
      return Number.isFinite(completed) && now - completed < 10 * 60 * 1000
    })
    .slice(0, 3)
  return [...active, ...recentTerminal]
}

/** Session-scoped job cards, docked at the end of the transcript. The agent
 *  turn may be long over; these keep updating from the job ledger (§12.2). */
export function SkillJobsDock({
  sessionId,
  receiptedJobIds,
}: {
  sessionId: string
  /** Jobs the transcript already shows a receipt for; the route supplies them
   *  because features do not reach across to each other (§4.2). */
  receiptedJobIds?: ReadonlySet<string>
}) {
  const { t } = useTranslation("jobs")
  useSkillJobLiveEvents()
  const jobs = useSessionSkillJobs(sessionId)

  // `undefined` for `now` on purpose: letting the default parameter read the
  // clock keeps Date.now() out of the render body, which is an impure call.
  const visible = visibleJobs(jobs.data ?? [], undefined, receiptedJobIds)
  if (visible.length === 0) return null

  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-n500">{t("dock.title")}</div>
      {visible.map((job) => (
        <SkillJobCard key={job.jobId} job={job} />
      ))}
    </div>
  )
}

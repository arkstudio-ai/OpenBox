import { useTranslation } from "react-i18next"
import { useSessionSkillJobs } from "@/features/jobs/api/jobs"
import { useSkillJobLiveEvents } from "@/features/jobs/hooks/useSkillJobLiveEvents"
import { TERMINAL_JOB_STATUSES, type SkillJobSnapshot } from "@/features/jobs/types"
import { SkillJobCard } from "./SkillJobCard"

/** Active jobs first; only recently finished terminal jobs stay visible.
 *  Exported for tests. */
export function visibleJobs(jobs: SkillJobSnapshot[], now = Date.now()): SkillJobSnapshot[] {
  const active = jobs.filter((j) => !TERMINAL_JOB_STATUSES.has(j.status))
  const recentTerminal = jobs
    .filter((j) => TERMINAL_JOB_STATUSES.has(j.status))
    .filter((j) => {
      const completed = j.completedAt ? Date.parse(j.completedAt) : NaN
      return Number.isFinite(completed) && now - completed < 10 * 60 * 1000
    })
    .slice(0, 3)
  return [...active, ...recentTerminal]
}

/** Session-scoped job cards, docked at the end of the transcript. The agent
 *  turn may be long over; these keep updating from the job ledger (§12.2). */
export function SkillJobsDock({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation("jobs")
  useSkillJobLiveEvents()
  const jobs = useSessionSkillJobs(sessionId)

  const visible = visibleJobs(jobs.data ?? [])
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

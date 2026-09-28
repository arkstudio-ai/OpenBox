// The scheduled jobs listed under the sidebar's "定时任务" row, each a link to
// its task page. The layout injects this into the workspace sidebar (features
// never import each other, ENGINEERING_SPEC §4). Hidden until there is a job.
import { useTranslation } from "react-i18next"
import { Link, useMatch } from "react-router"
import { cn } from "@/shared/lib/cn"
import { paths, routePatterns } from "@/shared/router/paths"
import { useCronJobs } from "@/features/cron/api/cron"
import { jobDotClass } from "@/features/cron/utils/jobs"
import type { CronJob } from "@/features/cron/types"

/** Enough to reach for; the tasks page lists the rest. */
const MAX_ROWS = 5

function JobRow({ job }: { job: CronJob }) {
  const active = useMatch(`${paths.app}/${routePatterns.cronJob}`)?.params.jobId === job.id
  return (
    <Link
      to={paths.cronJob(job.id)}
      aria-current={active ? "page" : undefined}
      className={cn(
        "text-md text-ink flex min-h-8 items-center gap-2 rounded-full py-1 ps-9 pe-2",
        active ? "bg-n200 font-medium" : "hover:bg-hairsoft",
      )}
    >
      <span className={cn("size-1.5 flex-none rounded-full", jobDotClass(job))} aria-hidden />
      <span className="min-w-0 flex-1 truncate">{job.name}</span>
    </Link>
  )
}

export function CronSidebarJobs() {
  const { t } = useTranslation("cron")
  const jobs = useCronJobs()
  const list = jobs.data ?? []
  if (list.length === 0) return null
  const rest = list.length - MAX_ROWS
  return (
    <div className="flex flex-col gap-px" data-testid="cron-sidebar-jobs">
      {list.slice(0, MAX_ROWS).map((job) => (
        <JobRow key={job.id} job={job} />
      ))}
      {rest > 0 && (
        <Link
          to={paths.cron}
          className="text-n600 hover:text-ink py-1 ps-9 text-xs underline-offset-2 hover:underline"
        >
          {t("nav.more", { count: rest })}
        </Link>
      )}
    </div>
  )
}

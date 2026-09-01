import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown } from "lucide-react"
import { Spinner } from "@/shared/ui/Spinner"
import { Menu, MenuItem } from "@/shared/ui/Menu"
import { formatRelative } from "@/shared/lib/format"
import {
  useCronJobs,
  useCronStatus,
  usePauseAllCronJobs,
  useResumeAllCronJobs,
} from "@/features/cron/api/cron"
import { useProjectOptions } from "@/features/cron/api/projects"
import { CronJobCard } from "@/features/cron/components/CronJobCard"
import { CronJobForm } from "@/features/cron/components/CronJobForm"
import { ChatCreateDialog } from "@/features/cron/components/ChatCreateDialog"
import type { CronJob } from "@/features/cron/types"

const pillCls = "min-h-8 rounded-full border border-hair px-3.5 text-xs text-n800 hover:bg-hairsoft"

/** Scheduler liveness footer: only alarming when actually unhealthy. */
function StatusLine() {
  const { t } = useTranslation("cron")
  const status = useCronStatus()
  if (!status.data) return null
  const s = status.data
  return (
    <div className="text-n500 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      <span className={s.healthy ? undefined : "text-danger"}>
        {s.healthy ? t("status.healthy") : t("status.unhealthy")}
      </span>
      {s.last_tick_at && <span>{t("status.lastTick", { when: formatRelative(s.last_tick_at) })}</span>}
      <span>{t("status.enabledCount", { count: s.enabled_jobs })}</span>
      {s.running_jobs > 0 && <span>{t("status.runningCount", { count: s.running_jobs })}</span>}
    </div>
  )
}

export function CronPage() {
  const { t } = useTranslation("cron")
  const jobs = useCronJobs()
  const projects = useProjectOptions()
  const pauseAll = usePauseAllCronJobs()
  const resumeAll = useResumeAllCronJobs()
  const [formOpen, setFormOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  const [editing, setEditing] = useState<CronJob | null>(null)

  const openManual = () => {
    setMenuOpen(false)
    setEditing(null)
    setFormOpen(true)
  }
  const openChat = () => {
    setMenuOpen(false)
    setChatOpen(true)
  }
  const openEdit = (job: CronJob) => {
    setEditing(job)
    setFormOpen(true)
  }

  const list = jobs.data ?? []
  const projectNames = new Map((projects.data ?? []).map((project) => [project.id, project.name]))
  const anyEnabled = list.some((j) => j.enabled)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
            className="bg-ink text-bg flex min-h-9 items-center gap-1.5 rounded-full px-4.5 text-sm"
          >
            {t("page.create")}
            <ChevronDown size={14} strokeWidth={2.4} />
          </button>
          <Menu open={menuOpen} onClose={() => setMenuOpen(false)} className="start-0 top-10 min-w-44">
            <MenuItem onClick={openManual}>{t("page.createManual")}</MenuItem>
            <MenuItem onClick={openChat}>{t("page.createChat")}</MenuItem>
          </Menu>
        </div>
        {list.length > 0 &&
          (anyEnabled ? (
            <button
              type="button"
              className={pillCls}
              disabled={pauseAll.isPending}
              onClick={() => pauseAll.mutate()}
            >
              {t("page.pauseAll")}
            </button>
          ) : (
            <button
              type="button"
              className={pillCls}
              disabled={resumeAll.isPending}
              onClick={() => resumeAll.mutate()}
            >
              {t("page.resumeAll")}
            </button>
          ))}
      </div>

      {jobs.isPending && (
        <div className="text-n600 flex items-center gap-2 py-6 text-sm">
          <Spinner />
          <span>{t("page.loading")}</span>
        </div>
      )}

      {jobs.isError && <span className="text-danger py-6 text-sm">{t("page.loadFailed")}</span>}

      {jobs.isSuccess && list.length === 0 && (
        <div className="border-hair bg-card flex flex-col gap-1 rounded-lg border px-4 py-6">
          <span className="text-ink text-base">{t("page.empty.title")}</span>
          <span className="text-n600 text-sm text-pretty">{t("page.empty.body")}</span>
        </div>
      )}

      {list.length > 0 && (
        <div className="flex flex-col gap-2.5">
          {list.map((job) => (
            <CronJobCard
              key={job.id}
              job={job}
              projectName={job.project_id ? projectNames.get(job.project_id) : null}
              onEdit={openEdit}
            />
          ))}
        </div>
      )}

      <StatusLine />

      <CronJobForm open={formOpen} onClose={() => setFormOpen(false)} job={editing} />
      <ChatCreateDialog open={chatOpen} onClose={() => setChatOpen(false)} />
    </div>
  )
}

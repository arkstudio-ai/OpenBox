import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { useLocation, useParams } from "react-router"
import { PanelLeft, PanelRight, Upload } from "lucide-react"
import { useCopy } from "@/shared/hooks/useCopy"
import { toast } from "@/shared/ui/Toast"
import { useSidebarLayout } from "../hooks/useSidebarLayout"
import { useProjectsQuery } from "../api/projects"
import { useSessionsQuery } from "../api/sessions"
import { paths } from "@/shared/router/paths"
import { EnvBadge } from "@/shared/ui/EnvBadge"

interface TopbarProps {
  panelOpen: boolean
  onTogglePanel: () => void
  /** Status widgets rendered before the panel toggle (e.g. the cron pill),
   *  injected by the assembly layer to keep features decoupled. */
  statusSlot?: React.ReactNode
}

export function Topbar({ panelOpen, onTogglePanel, statusSlot }: TopbarProps) {
  const { t } = useTranslation("workspace")
  const { sessionId } = useParams()
  const location = useLocation()
  const sidebar = useSidebarLayout()
  const sessions = useSessionsQuery()
  const projects = useProjectsQuery()
  const { copy } = useCopy()

  const isSettings = location.pathname.includes("/settings")
  const isCron = location.pathname.includes("/cron")
  const isResources = location.pathname.includes("/resources")
  const isAuthCenter = location.pathname.includes("/auth-center")
  const isBilling =
    location.pathname === paths.billing() || location.pathname.startsWith(`${paths.billing()}/`)
  const session = useMemo(
    () => (sessions.data ?? []).find((s) => s.id === sessionId) ?? null,
    [sessions.data, sessionId],
  )
  const project = useMemo(
    () => (projects.data ?? []).find((p) => p.id === session?.project_id) ?? null,
    [projects.data, session],
  )

  // Pages that are not a conversation name themselves; everything else is a
  // chat, and falls back to its title and project.
  const standalone = isBilling
    ? { title: t("billing"), subtitle: "" }
    : isSettings
      ? { title: t("settings"), subtitle: t("settings:subtitle", { ns: "settings" }) }
      : isCron
        ? { title: t("scheduledTasks"), subtitle: t("scheduledTasksHint") }
        : isResources
          ? { title: t("resourceCenter"), subtitle: t("resourceCenterHint") }
          : isAuthCenter
            ? { title: t("authCenter"), subtitle: t("authCenterHint") }
            : null
  const title = standalone?.title ?? session?.title ?? t("untitledChat")
  const subtitle = standalone?.subtitle ?? project?.name ?? t("unsorted")

  const share = () => {
    copy(window.location.href)
    toast("info", t("shareCopied"))
  }

  return (
    <div className="flex h-15.5 flex-none items-center gap-2 px-3 sm:gap-3 sm:ps-6.5 sm:pe-4.5">
      {!sidebar.open && (
        <button
          type="button"
          className="text-n700 hover:bg-n200 flex size-8 flex-none items-center justify-center rounded-full"
          onClick={sidebar.toggle}
          title={t("expand")}
          aria-label={t("expand")}
        >
          <PanelLeft size={17} strokeWidth={2.4} />
        </button>
      )}
      <div className="flex min-w-0 flex-1 items-baseline gap-2.5 overflow-hidden">
        <span className="max-w-3/5 flex-none truncate text-lg font-medium">{title}</span>
        <span className="text-n600 hidden min-w-0 flex-none truncate text-sm sm:block">{subtitle}</span>
      </div>
      <EnvBadge />
      {!isSettings && !isResources && !isBilling && statusSlot}
      {session && (
        <button
          type="button"
          className="text-n700 hover:bg-hairsoft flex size-8 flex-none items-center justify-center rounded-full"
          title={t("share")}
          aria-label={t("share")}
          onClick={share}
        >
          <Upload size={16} strokeWidth={2.4} />
        </button>
      )}
      {!panelOpen && !standalone && (
        <button
          type="button"
          className="text-n700 hover:bg-n200 flex size-8 flex-none items-center justify-center rounded-full"
          onClick={onTogglePanel}
          title={t("openPanel")}
          aria-label={t("openPanel")}
        >
          <PanelRight size={17} strokeWidth={2.4} />
        </button>
      )}
    </div>
  )
}

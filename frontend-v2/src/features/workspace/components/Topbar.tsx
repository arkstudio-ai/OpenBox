import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ArrowLeft, PanelLeft, PanelRight, Upload } from "lucide-react"
import { useCopy } from "@/shared/hooks/useCopy"
import { toast } from "@/shared/ui/Toast"
import { useSidebarLayout } from "../hooks/useSidebarLayout"
import { useTopbarHeading } from "../hooks/useTopbarHeading"
import { useSessionsQuery } from "../api/sessions"
import { useWorkspaceUi } from "../stores/ui"
import type { StandalonePage } from "../lib/standalonePage"
import { paths } from "@/shared/router/paths"
import { EnvBadge } from "@/shared/ui/EnvBadge"

interface TopbarProps {
  panelOpen: boolean
  onTogglePanel: () => void
  /** Status widgets rendered before the panel toggle (e.g. the cron pill),
   *  injected by the assembly layer to keep features decoupled. */
  statusSlot?: React.ReactNode
}

// Pages whose status widgets would be noise: nothing on them runs.
const QUIET_PAGES: ReadonlySet<StandalonePage> = new Set(["settings", "resources", "billing"])

export function Topbar({ panelOpen, onTogglePanel, statusSlot }: TopbarProps) {
  const { t } = useTranslation("workspace")
  const sidebar = useSidebarLayout()
  const sessions = useSessionsQuery()
  const lastSessionId = useWorkspaceUi((s) => s.lastSessionId)
  const { page, session, title, subtitle } = useTopbarHeading()
  const { copy } = useCopy()

  // "Back to chat" returns to the conversation the person left, as long as it
  // still exists; otherwise to a fresh one. Not history.back(): after a sign-in
  // redirect or a pasted link there is nothing sensible behind this page.
  const lastSessionAlive = (sessions.data ?? []).some((s) => s.id === lastSessionId)
  const backTo = lastSessionAlive && lastSessionId ? paths.chat(lastSessionId) : paths.newChat()

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
      {page && (
        <Link
          to={backTo}
          title={t("backToChat")}
          aria-label={t("backToChat")}
          className="text-a700 hover:bg-hairsoft flex h-8 flex-none items-center gap-1.5 rounded-full px-2.5 text-sm"
        >
          <ArrowLeft size={15} strokeWidth={2.4} aria-hidden />
          <span className="hidden sm:inline">{t("backToChat")}</span>
        </Link>
      )}
      <EnvBadge />
      {!(page && QUIET_PAGES.has(page)) && statusSlot}
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
      {!panelOpen && !page && (
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

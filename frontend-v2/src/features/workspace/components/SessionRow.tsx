import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { Clock, Trash2 } from "lucide-react"
import type { Session } from "@/shared/types/api"
import { Spinner } from "@/shared/ui/Spinner"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { useWorkspaceUi } from "../stores/ui"
import { useAuthStore } from "@/shared/api/auth-store"

interface SessionRowProps {
  session: Session
  active: boolean
  onAskDelete: () => void
}

export function SessionRow({ session, active, onAskDelete }: SessionRowProps) {
  const { t } = useTranslation("workspace")
  const selectProject = useWorkspaceUi((s) => s.selectProject)
  const currentUserId = useAuthStore((s) => s.user?.id)
  const [hover, setHover] = useState(false)
  const busy = session.status === "busy" || session.status === "finalizing" || session.status === "compacting"

  return (
    <div
      className={cn(
        "flex min-h-8 items-center gap-1.5 rounded-full py-1 ps-7.5 pe-2",
        active ? "bg-n200 font-medium" : hover ? "bg-hairsoft" : undefined,
      )}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <Link
        to={paths.chat(session.id)}
        // Opening a chat makes its project the current one for "new chat".
        onClick={() => selectProject(session.project_id ?? null)}
        className="flex min-w-0 flex-1 items-center gap-1.5 text-base text-ink"
      >
        {session.kind === "cron" && (
          <Clock
            size={13}
            strokeWidth={2.2}
            className="flex-none text-n600"
            aria-label={t("cronRun")}
          />
        )}
        <span className="min-w-0 flex-1 truncate">
          {session.title || t("untitledChat")}
          {session.user_id && session.user_id !== currentUserId && session.owner_username
            ? ` · ${session.owner_username}`
            : ""}
        </span>
      </Link>
      {busy && <Spinner className="size-3 flex-none" />}
      {hover && (!session.user_id || session.user_id === currentUserId) && (
        <button
          type="button"
          title={t("common:action.delete", { ns: "common" })}
          aria-label={t("common:action.delete", { ns: "common" })}
          className="flex size-5.5 flex-none items-center justify-center rounded-full text-n700 hover:bg-n200"
          onClick={onAskDelete}
        >
          <Trash2 size={13.5} strokeWidth={2.4} />
        </button>
      )}
    </div>
  )
}

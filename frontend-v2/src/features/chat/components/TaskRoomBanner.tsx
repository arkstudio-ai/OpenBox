// "你在任务间：「…」· 回到总助理" — the execution page says which room this is.
//
// A task's execution conversation and the personal assistant's own conversation
// look the same: same bubbles, same composer. People could not tell whether
// they were talking to the assistant or to one task (2026-11-02), so this
// banner sits at the top of every task-linked page and names the room, its
// state and the one way back to the assistant (VOICE_SEAM_PLAN §11.3).
import { DoorOpen } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
import type { Session } from "@/shared/types/api"
import { cn } from "@/shared/lib/cn"
import { roomState } from "../lib/room-state"

export function TaskRoomBanner({ session, className }: { session?: Session; className?: string }) {
  const { t } = useTranslation("chat")
  const control = session?.task_control
  if (!session?.assistant_managed || session.kind === "assistant" || !control) return null
  const title = control.task_title || session.title
  const state = roomState(control)
  const alert = state === "effect_unknown" || state === "waiting" || state === "error"
  return (
    <div
      role="region"
      aria-label={t("room.here", { title })}
      data-testid="task-room-banner"
      className={cn("bg-a100 border-hair flex items-center gap-2 border-b px-3 py-1.5 text-sm", className)}
    >
      <DoorOpen size={15} strokeWidth={2.2} aria-hidden className="text-a700 flex-none" />
      <span className="text-ink min-w-0 flex-1 truncate">
        {t("room.here", { title })}
        <span className={cn("ms-2 text-xs", alert ? "text-dangerink" : "text-n600")}>{t(`room.state.${state}`)}</span>
      </span>
      <Link to={paths.assistant} className="text-a700 hover:bg-hairsoft flex-none rounded-full px-2 py-0.5 text-xs font-medium">
        {t("room.back")}
      </Link>
    </div>
  )
}

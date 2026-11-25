import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { Ellipsis } from "lucide-react"
import type { Session } from "@/shared/types/api"
import { Menu, MenuItem } from "@/shared/ui/Menu"
import { Spinner } from "@/shared/ui/Spinner"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { useWorkspaceUi } from "../stores/ui"

interface SessionRowProps {
  session: Session
  active: boolean
  owned: boolean
  onAskDelete: () => void
  onAskRename: () => void
  selection?: { checked: boolean; disabled: boolean; toggle: () => void }
}

export function SessionRow({ session, active, owned, onAskDelete, onAskRename, selection }: SessionRowProps) {
  const { t } = useTranslation("workspace")
  const selectProject = useWorkspaceUi((s) => s.selectProject)
  const [menu, setMenu] = useState(false)
  const busy = session.status === "busy" || session.status === "finalizing" || session.status === "compacting"
  const title = session.title || t("untitledChat")
  const label = !owned && session.owner_username ? `${title} · ${session.owner_username}` : title
  const text = <span className="min-w-0 flex-1 truncate">{label}</span>

  return (
    <div className={cn(
      "group hover:bg-hairsoft relative flex min-h-8 items-center gap-1.5 rounded-full py-1 ps-7.5 pe-2",
      (active || selection?.checked) && "bg-n200 font-medium",
    )}>
      {selection ? (
        <label className="text-ink flex min-w-0 flex-1 items-center gap-2 text-base">
          {owned && <input type="checkbox" checked={selection.checked} disabled={selection.disabled}
            onChange={selection.toggle} className="accent-a700" />}
          {text}
        </label>
      ) : (
        <Link to={paths.chat(session.id)}
          onClick={() => selectProject(session.project_id ?? null)}
          className="text-ink flex min-w-0 flex-1 items-center gap-1.5 text-base">
          {text}
        </Link>
      )}
      {busy && <Spinner className="size-3 flex-none" />}
      {owned && !selection && (
        <>
          <button type="button" aria-haspopup="menu" aria-expanded={menu}
            title={t("sessionActions")} aria-label={t("sessionActions")}
            className="text-n700 hover:bg-n200 flex size-6 flex-none items-center justify-center rounded-full"
            onClick={() => setMenu(!menu)}>
            <Ellipsis size={15} />
          </button>
          <Menu open={menu} onClose={() => setMenu(false)} className="end-1 top-full min-w-28">
            <MenuItem onClick={() => { setMenu(false); onAskRename() }}>{t("renameChat")}</MenuItem>
            <MenuItem danger onClick={() => { setMenu(false); onAskDelete() }}>
              {t("common:action.delete", { ns: "common" })}
            </MenuItem>
          </Menu>
        </>
      )}
    </div>
  )
}

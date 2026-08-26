// One row in the middle column. Presentational: every action is a callback,
// the rename editor is the row's only internal state.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Bot, PencilLine, Trash2 } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { KIND_ICON } from "../constants"
import type { Resource } from "../types"

interface Props {
  resource: Resource
  active: boolean
  checked: boolean
  onOpen: (id: string) => void
  onToggle: (id: string, checked: boolean) => void
  onRename: (id: string, name: string) => void
  onDelete: (resource: Resource) => void
}

export function ResourceRow({ resource, active, checked, onOpen, onToggle, onRename, onDelete }: Props) {
  const { t } = useTranslation("resources")
  const [draft, setDraft] = useState<string | null>(null)
  const Icon = KIND_ICON[resource.kind]

  const commit = () => {
    const name = (draft ?? "").trim()
    setDraft(null)
    if (name && name !== resource.name) onRename(resource.id, name)
  }

  if (draft !== null) {
    return (
      <div className="bg-n200 flex h-9 items-center gap-2 rounded-lg px-2">
        <Icon className="text-n600 size-3.5 flex-none" strokeWidth={2} />
        <input
          value={draft}
          aria-label={t("actions.rename")}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit()
            if (e.key === "Escape") setDraft(null)
          }}
          onBlur={commit}
          className="text-ink min-w-0 flex-1 bg-transparent text-xs outline-none"
          autoFocus
        />
      </div>
    )
  }

  return (
    <div
      className={cn(
        "group/row relative flex h-9 items-center rounded-lg",
        active ? "bg-n200" : "hover:bg-hairsoft",
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        aria-label={t("actions.select")}
        onChange={(e) => onToggle(resource.id, e.target.checked)}
        className={cn(
          "accent-ink absolute start-2 z-10 size-3",
          checked ? "opacity-100" : "opacity-0 group-hover/row:opacity-100",
        )}
      />
      <button
        type="button"
        onClick={() => onOpen(resource.id)}
        title={resource.name}
        className="flex h-9 min-w-0 flex-1 items-center gap-2 ps-7 pe-14 text-start"
      >
        <Icon className="text-n600 size-3.5 flex-none" strokeWidth={2} />
        <span className={cn("min-w-0 flex-1 truncate text-xs", active ? "text-ink" : "text-n800")}>
          {resource.name}
        </span>
      </button>

      {resource.source === "agent" && (
        <span
          title={t("source.agent")}
          className="text-n500 pointer-events-none absolute end-2 flex items-center transition-opacity group-hover/row:opacity-0"
        >
          <Bot className="size-3.5" strokeWidth={1.8} />
        </span>
      )}

      <div className="absolute end-1 flex items-center gap-0.5 opacity-0 transition-opacity group-hover/row:opacity-100">
        <button
          type="button"
          onClick={() => setDraft(resource.name)}
          aria-label={t("actions.rename")}
          title={t("actions.rename")}
          className="text-n600 hover:bg-n300 hover:text-ink flex size-6 items-center justify-center rounded-md"
        >
          <PencilLine className="size-3.5" strokeWidth={1.8} />
        </button>
        <button
          type="button"
          onClick={() => onDelete(resource)}
          aria-label={t("actions.delete")}
          title={t("actions.delete")}
          className="text-n600 hover:bg-n300 hover:text-dangerink flex size-6 items-center justify-center rounded-md"
        >
          <Trash2 className="size-3.5" strokeWidth={1.8} />
        </button>
      </div>
    </div>
  )
}

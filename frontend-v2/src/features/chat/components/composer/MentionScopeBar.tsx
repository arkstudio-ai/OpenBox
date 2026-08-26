// Scope strip at the top of the "@" menu: which project's resources are
// listed, and whose files they are. The menu opens on the conversation's own
// project, and this is the way out of it.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, ChevronDown } from "lucide-react"
import { Menu } from "@/shared/ui/Menu"
import { cn } from "@/shared/lib/cn"
import type { MentionScope } from "../../hooks/useMentionMenu"

interface Props {
  scope: MentionScope
}

const SOURCES = ["all", "user", "agent"] as const
const SOURCE_KEY: Record<(typeof SOURCES)[number], string> = {
  all: "composer.mention.sourceAll",
  user: "composer.mention.sourceUser",
  agent: "composer.mention.sourceAgent",
}

/** Matches the backend's "every project" sentinel. */
const ALL_SCOPE = "all"

const CHIP = "flex h-6.5 items-center gap-1 rounded-full px-2 text-2xs"

export function MentionScopeBar({ scope }: Props) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const current = scope.projects.find((p) => p.id === scope.project)
  const label = current?.name ?? t("composer.mention.allProjects")

  return (
    <div className="border-hair flex items-center gap-1 border-b px-1.5 pb-1.5">
      <div className="relative">
        <button
          type="button"
          onMouseDown={(e) => {
            // Keeps the textarea focused — the caret restore depends on it.
            e.preventDefault()
            setOpen((v) => !v)
          }}
          className={cn(CHIP, "bg-n200 text-ink font-medium")}
        >
          <span className="max-w-28 truncate">{label}</span>
          <ChevronDown className="size-3 flex-none" strokeWidth={2.2} />
        </button>
        <Menu open={open} onClose={() => setOpen(false)} className="start-0 bottom-8 w-44">
          {[{ id: ALL_SCOPE, name: t("composer.mention.allProjects") }, ...scope.projects].map((p) => (
            <button
              key={p.id}
              type="button"
              role="menuitemradio"
              aria-checked={p.id === scope.project}
              onMouseDown={(e) => {
                e.preventDefault()
                scope.setProject(p.id)
                setOpen(false)
              }}
              className={cn(
                "flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-start text-xs",
                p.id === scope.project ? "bg-n200 text-ink" : "text-n800 hover:bg-hairsoft",
              )}
            >
              <span className="min-w-0 flex-1 truncate">{p.name}</span>
              {p.id === scope.project && <Check className="size-3 flex-none" strokeWidth={2.4} />}
            </button>
          ))}
        </Menu>
      </div>

      <span className="bg-hair mx-0.5 h-3.5 w-px flex-none" aria-hidden />

      {SOURCES.map((value) => (
        <button
          key={value}
          type="button"
          onMouseDown={(e) => {
            e.preventDefault()
            scope.setSource(value)
          }}
          className={cn(CHIP, scope.source === value ? "bg-n200 text-ink" : "text-n600 hover:bg-hairsoft")}
        >
          {t(SOURCE_KEY[value])}
        </button>
      ))}
    </div>
  )
}

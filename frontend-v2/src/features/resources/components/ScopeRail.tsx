// Left rail: the first-level filter is the project, the second level is who
// produced the file. The source rows appear under the open project only —
// that nesting *is* the "project → user input / model output" hierarchy.
import { useTranslation } from "react-i18next"
import { Folder, FolderOpen, Inbox, Layers } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { ALL_PROJECTS, NO_PROJECT, SOURCE_FILTERS, SOURCE_ICON, SOURCE_LABEL } from "../constants"
import type { ResourceProject, SourceFilter } from "../types"

interface Props {
  projects: ResourceProject[]
  project: string
  source: SourceFilter
  onPickProject: (id: string) => void
  onPickSource: (source: SourceFilter) => void
}

function SourceRows({ active, onPick }: { active: SourceFilter; onPick: (source: SourceFilter) => void }) {
  const { t } = useTranslation("resources")
  return (
    <div className="border-hair ms-6.5 mb-1 flex flex-col gap-0.5 border-s ps-2">
      {SOURCE_FILTERS.map((value) => {
        const Icon = value === "all" ? Layers : SOURCE_ICON[value]
        return (
          <button
            key={value}
            type="button"
            onClick={() => onPick(value)}
            className={cn(
              "flex h-7 items-center gap-2 rounded-full px-2 text-xs",
              active === value ? "bg-n200 text-ink" : "text-n700 hover:bg-hairsoft",
            )}
          >
            <Icon className="size-3.5 flex-none" strokeWidth={2} />
            <span className="truncate">{t(SOURCE_LABEL[value])}</span>
          </button>
        )
      })}
    </div>
  )
}

export function ScopeRail({ projects, project, source, onPickProject, onPickSource }: Props) {
  const { t } = useTranslation("resources")

  const rows = [
    { id: ALL_PROJECTS, name: t("scope.allProjects"), icon: Layers },
    ...projects.map((p) => ({ id: p.id, name: p.name, icon: Folder })),
    { id: NO_PROJECT, name: t("scope.unfiled"), icon: Inbox },
  ]

  return (
    <nav className="scr border-hair flex w-52 flex-none flex-col gap-0.5 overflow-y-auto border-e px-2.5 py-3">
      <span className="text-2xs text-n600 px-2 pb-1.5 font-medium tracking-wide">{t("scope.title")}</span>
      {rows.map((row) => {
        const active = row.id === project
        const Icon = active && row.icon === Folder ? FolderOpen : row.icon
        return (
          <div key={row.id}>
            <button
              type="button"
              onClick={() => onPickProject(row.id)}
              className={cn(
                "text-md flex h-9 w-full items-center gap-2.5 rounded-full px-2",
                active ? "bg-n200 text-ink font-medium" : "text-ink hover:bg-hairsoft",
              )}
            >
              <Icon className="text-n700 size-4 flex-none" strokeWidth={2} />
              <span className="min-w-0 flex-1 truncate text-start">{row.name}</span>
            </button>
            {active && <SourceRows active={source} onPick={onPickSource} />}
          </div>
        )
      })}
    </nav>
  )
}

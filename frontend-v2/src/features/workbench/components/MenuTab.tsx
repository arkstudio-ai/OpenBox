// The "new tab" menu page: four entry rows (review / terminal / browser / files)
// with a live hint column — pending-review count, the running sandbox name, etc.
// Clicking a row opens (or converts the current menu tab into) that kind.
import { useTranslation } from "react-i18next"
import { usePanelStore } from "@/features/workbench/stores/panel"
import { TAB_GLYPH } from "@/features/workbench/utils/glyphs"
import { useDiffQuery } from "@/features/workbench/api/diff"
import { useRunningContainer } from "@/features/workbench/api/containers"
import { useSessionWorkdir } from "@/features/workbench/api/workdir"
import type { TabKind } from "@/features/workbench/stores/panel"

interface MenuTabProps {
  sessionId: string | null
  projectName: string | null
}

const ROWS: TabKind[] = ["review", "terminal", "preview", "browser", "files", "desktop", "cron"]

export function MenuTab({ sessionId, projectName }: MenuTabProps) {
  const { t } = useTranslation("workbench")
  const openKind = usePanelStore((s) => s.openKind)
  const diff = useDiffQuery(sessionId)
  const running = useRunningContainer()
  const workdir = useSessionWorkdir(sessionId)

  const pending = diff.data?.length ?? 0
  // Hints stay human: the sandbox's machine id (a Wuying desktop id, a
  // container hash) is meaningless here — files shows the project directory,
  // terminal just whether a sandbox is up.
  const hintFor = (kind: TabKind): string => {
    if (kind === "review") return pending > 0 ? t("menu.pending", { count: pending }) : t("menu.clean")
    if (kind === "terminal" || kind === "preview") return running ? t("menu.online") : ""
    if (kind === "files") return projectName ?? (workdir ? t("files.projectRoot") : "")
    return ""
  }

  return (
    <div className="scr flex min-h-0 flex-1 flex-col gap-0.5 overflow-auto px-3 pt-1 pb-4">
      {ROWS.map((kind) => (
        <button
          key={kind}
          type="button"
          onClick={() => openKind(kind)}
          className="hover:bg-hairsoft flex min-h-11.5 items-center gap-3 rounded-full px-3.5 text-start"
        >
          <span className="border-hair text-n700 flex size-7 flex-none items-center justify-center rounded-full border font-mono text-xs">
            {TAB_GLYPH[kind]}
          </span>
          <span className="text-base">{t(`menu.${kind}`)}</span>
          <span className="text-n600 ms-auto ps-3 text-xs">{hintFor(kind)}</span>
        </button>
      ))}
    </div>
  )
}

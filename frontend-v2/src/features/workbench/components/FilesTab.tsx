// Files tab: a header (sandbox › file · copy · list toggle), the content viewer,
// and the resizable tree column. In a narrow panel the tree replaces the viewer.
import type { MouseEvent as ReactMouseEvent } from "react"
import { useTranslation } from "react-i18next"
import { Copy, List } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { useCopy } from "@/shared/hooks/useCopy"
import { toast } from "@/shared/ui/Toast"
import { usePanelStore } from "@/features/workbench/stores/panel"
import { useRunningContainer } from "@/features/workbench/api/containers"
import { useSessionWorkdir } from "@/features/workbench/api/workdir"
import {
  projectParentPath,
  projectRelativePath,
  resolveProjectPath,
} from "@/features/workbench/utils/project-path"
import { EmptyState } from "./EmptyState"
import { FileViewer } from "./FileViewer"
import { FilesTree } from "./FilesTree"

interface FilesTabProps {
  narrow: boolean
  sessionId: string | null
  projectName: string | null
  projectDirectory: string | null
}

export function FilesTab({ narrow, sessionId, projectName, projectDirectory }: FilesTabProps) {
  const { t } = useTranslation("workbench")
  const running = useRunningContainer()
  const containerId = running?.id ?? null
  // Scope the tree to the session's project directory — /workspace is the
  // agent's whole activity space, not the project being worked on.
  const workdir = useSessionWorkdir(sessionId)
  const openFile = usePanelStore((s) => s.openFile)
  const setOpenFile = usePanelStore((s) => s.setOpenFile)
  const treeOpen = usePanelStore((s) => s.treeOpen)
  const treeWidth = usePanelStore((s) => s.treeWidth)
  const toggleTree = usePanelStore((s) => s.toggleTree)
  const setTreeWidth = usePanelStore((s) => s.setTreeWidth)
  const { copy } = useCopy()

  if (!containerId) {
    return <EmptyState title={t("sandbox.none")} hint={t("terminal.emptyHint")} />
  }
  if (sessionId && !workdir) {
    // One round-trip while the session detail loads; rooting at /workspace in
    // the meantime would flash the whole sandbox before snapping to the project.
    return <EmptyState title={t("files.pickFile")} />
  }

  const root = workdir ?? projectDirectory
  if (!root) {
    return <EmptyState title={t("files.pickFile")} />
  }
  const fullPath = resolveProjectPath(openFile, root)
  const initialCwd = fullPath ? projectParentPath(fullPath, root) : root
  const rootName = projectName ?? t("files.projectRoot")

  const showTree = treeOpen
  const showViewer = !(narrow && treeOpen)
  const relativePath = projectRelativePath(fullPath, root)
  const name = relativePath ?? ""

  const startTreeDrag = (e: ReactMouseEvent) => {
    e.preventDefault()
    const x0 = e.clientX
    const w0 = usePanelStore.getState().treeWidth
    const move = (ev: MouseEvent) => setTreeWidth(w0 - (ev.clientX - x0))
    const up = () => {
      window.removeEventListener("mousemove", move)
      window.removeEventListener("mouseup", up)
      document.body.style.userSelect = ""
    }
    window.addEventListener("mousemove", move)
    window.addEventListener("mouseup", up)
    document.body.style.userSelect = "none"
  }

  const onCopy = () => {
    if (!fullPath) return
    copy(relativePath ?? "")
    toast("info", t("files.pathCopied"))
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none items-center gap-2 px-3 pb-2.5">
        {!narrow && <span className="text-n600 flex-none text-sm">{`${rootName} ›`}</span>}
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{name || t("files.pickFile")}</span>
        <button
          type="button"
          onClick={onCopy}
          disabled={!fullPath}
          title={t("files.copyPath")}
          aria-label={t("files.copyPath")}
          className="text-n700 hover:bg-hairsoft flex size-7.5 flex-none items-center justify-center rounded-full disabled:opacity-40"
        >
          <Copy size={14} strokeWidth={2.2} />
        </button>
        <button
          type="button"
          onClick={toggleTree}
          title={t("files.fileList")}
          aria-label={t("files.fileList")}
          className={cn(
            "hover:bg-n200 flex size-7.5 flex-none items-center justify-center rounded-full",
            treeOpen ? "text-a700" : "text-n700",
          )}
        >
          <List size={15} strokeWidth={2.4} />
        </button>
      </div>

      <div className="flex min-h-0 flex-1 gap-2.5 px-3 pb-3">
        {showViewer && (
          <div className="scr border-hair bg-card min-w-0 flex-1 overflow-auto rounded-2xl border p-4">
            <FileViewer containerId={containerId} path={fullPath} />
          </div>
        )}
        {showTree && (
          <FilesTree
            key={`${containerId}:${initialCwd}`}
            containerId={containerId}
            root={root}
            rootLabel={rootName}
            initialCwd={initialCwd}
            openFile={fullPath}
            onOpenFile={setOpenFile}
            width={treeWidth}
            narrow={narrow}
            onStartDrag={startTreeDrag}
          />
        )}
      </div>
    </div>
  )
}

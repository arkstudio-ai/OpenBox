import { useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { Blocks, Clock, Layers, PanelLeft, Plus, Search } from "lucide-react"
import { BrandMark } from "@/shared/ui/BrandMark"
import { paths } from "@/shared/router/paths"
import { useProjectsQuery, useCreateProject } from "../api/projects"
import { useSessionsQuery } from "../api/sessions"
import { useWorkspaceUi } from "../stores/ui"
import { ProjectTree } from "./ProjectTree"
import { UserRow } from "./UserRow"
import { WorkspaceSwitcher } from "./WorkspaceSwitcher"

export function Sidebar() {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  const width = useWorkspaceUi((s) => s.sidebarWidth)
  const collapsed = useWorkspaceUi((s) => s.sidebarCollapsed)
  const toggleSidebar = useWorkspaceUi((s) => s.toggleSidebar)
  const setSidebarWidth = useWorkspaceUi((s) => s.setSidebarWidth)

  const projects = useProjectsQuery()
  const sessions = useSessionsQuery()
  const createProject = useCreateProject()
  const selectedProject = useWorkspaceUi((s) => s.selectedProject)
  // A stale selection (deleted project) must not point the resource centre
  // at a project that no longer exists.
  const activeProject =
    selectedProject && (projects.data ?? []).some((p) => p.id === selectedProject)
      ? selectedProject
      : null

  const [draftOpen, setDraftOpen] = useState(false)
  const [draftName, setDraftName] = useState("")
  const [query, setQuery] = useState("")
  const dragStart = useRef<{ x: number; w: number } | null>(null)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const all = sessions.data ?? []
    if (!q) return all
    return all.filter((s) => s.title.toLowerCase().includes(q))
  }, [sessions.data, query])

  const commitDraft = () => {
    const name = draftName.trim()
    setDraftOpen(false)
    setDraftName("")
    if (name) createProject.mutate(name)
  }

  const startDrag = (e: React.MouseEvent) => {
    e.preventDefault()
    dragStart.current = { x: e.clientX, w: width }
    const move = (ev: MouseEvent) => {
      if (dragStart.current) setSidebarWidth(dragStart.current.w + ev.clientX - dragStart.current.x)
    }
    const up = () => {
      dragStart.current = null
      window.removeEventListener("mousemove", move)
      window.removeEventListener("mouseup", up)
      document.body.style.userSelect = ""
    }
    window.addEventListener("mousemove", move)
    window.addEventListener("mouseup", up)
    document.body.style.userSelect = "none"
  }

  if (collapsed) return null

  return (
    <aside
      className="relative flex min-h-0 flex-none flex-col border-e border-hair bg-rail"
      style={{ width }}
    >
      <div className="flex min-h-0 flex-1 flex-col ps-4.5 pe-3 pt-3.5 pb-2.5">
        <div className="flex items-center gap-2.5 pt-0.5 pb-4">
          <BrandMark className="min-w-0 flex-1" />
          <button
            type="button"
            className="flex size-7.5 flex-none items-center justify-center rounded-full text-n700 hover:bg-hairsoft"
            onClick={toggleSidebar}
            title={t("collapse")}
            aria-label={t("collapse")}
          >
            <PanelLeft size={17} strokeWidth={2.4} />
          </button>
        </div>

        <WorkspaceSwitcher />

        {/* DEEIX-style nav rows: left-aligned, icon column, the primary action
            wears a round tinted icon chip instead of a filled pill. */}
        <button
          type="button"
          // The sidebar's primary action opens a project draft; chats are
          // started inside a project from its own row.
          onClick={() => setDraftOpen(true)}
          className="group flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base font-medium text-ink hover:bg-hairsoft"
        >
          <span className="flex size-7 flex-none items-center justify-center rounded-full bg-n200 transition-transform duration-150 group-hover:scale-105">
            <Plus size={15} strokeWidth={2.5} />
          </span>
          {t("newProject")}
        </button>

        <div className="flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 focus-within:bg-hairsoft hover:bg-hairsoft">
          <span className="flex size-7 flex-none items-center justify-center">
            <Search size={16} strokeWidth={2.1} className="text-ink" aria-hidden />
          </span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("search")}
            className="min-w-0 flex-1 bg-transparent pe-2 text-base text-ink outline-none placeholder:text-n600"
          />
        </div>

        <button
          type="button"
          // Opens on the project in view, which is the one whose files the
          // person was just looking at.
          onClick={() => navigate(paths.resources(activeProject ?? undefined))}
          className="mt-2.5 flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base text-ink hover:bg-hairsoft"
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <Layers size={16} strokeWidth={2.1} />
          </span>
          {t("resourceCenter")}
        </button>

        <button
          type="button"
          onClick={() => navigate(paths.skills)}
          className="flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base text-ink hover:bg-hairsoft"
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <Blocks size={16} strokeWidth={2.1} />
          </span>
          {t("skillCenter")}
        </button>

        <button
          type="button"
          onClick={() => navigate(paths.cron)}
          className="mb-1.5 flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base text-ink hover:bg-hairsoft"
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <Clock size={16} strokeWidth={2.1} />
          </span>
          {t("scheduledTasks")}
        </button>

        {draftOpen && (
          <div className="mb-1 flex flex-none items-center gap-2 rounded-full border border-hair px-3.5 py-2">
            <span className="size-1.75 rounded-full bg-accent" aria-hidden />
            <input
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitDraft()
                if (e.key === "Escape") setDraftOpen(false)
              }}
              onBlur={commitDraft}
              placeholder={t("projectName")}
              className="min-w-0 flex-1 border-none bg-transparent text-base text-ink outline-none"
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
            />
          </div>
        )}

        <div className="scr -mx-1 flex min-h-0 flex-1 flex-col gap-0.5 overflow-x-hidden overflow-y-auto p-1">
          <ProjectTree projects={projects.data ?? []} sessions={filtered} searching={query.trim().length > 0} />
        </div>

        <UserRow sessionCount={(sessions.data ?? []).length} />
      </div>
      <button
        type="button"
        aria-hidden
        tabIndex={-1}
        onMouseDown={startDrag}
        className="absolute top-0 bottom-0 -end-1 z-6 w-2 cursor-col-resize"
      />
    </aside>
  )
}

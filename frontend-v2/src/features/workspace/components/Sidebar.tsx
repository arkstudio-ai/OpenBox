import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { useLocation, useMatch, useNavigate } from "react-router"
import { Blocks, Clock, CreditCard, KeyRound, Layers, PanelLeft, Plus, Search } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { BrandMark } from "@/shared/ui/BrandMark"
import { paths } from "@/shared/router/paths"
import { useProjectsQuery, useCreateProject } from "../api/projects"
import { useSessionsQuery } from "../api/sessions"
import { useWorkspaceUi } from "../stores/ui"
import { ProjectTree } from "./ProjectTree"
import { UserRow } from "./UserRow"
import { WorkspaceSwitcher } from "./WorkspaceSwitcher"
import { useSidebarLayout } from "../hooks/useSidebarLayout"

export function Sidebar() {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  const isBilling = useMatch(`${paths.billing()}/*`) !== null
  const width = useWorkspaceUi((s) => s.sidebarWidth)
  const { compact, open, close } = useSidebarLayout()
  const closeMobile = useWorkspaceUi((s) => s.closeMobileSidebar)
  const location = useLocation()
  const navigationRef = useRef<HTMLElement>(null)
  const setSidebarWidth = useWorkspaceUi((s) => s.setSidebarWidth)

  const projects = useProjectsQuery()
  const sessions = useSessionsQuery()
  const createProject = useCreateProject()
  const selectedProject = useWorkspaceUi((s) => s.selectedProject)
  // A stale selection (deleted project) must not point the resource centre
  // at a project that no longer exists.
  const activeProject =
    selectedProject && (projects.data ?? []).some((p) => p.id === selectedProject) ? selectedProject : null

  const [draftOpen, setDraftOpen] = useState(false)
  const [draftName, setDraftName] = useState("")
  const [query, setQuery] = useState("")
  const dragStart = useRef<{ x: number; w: number } | null>(null)

  useEffect(() => { closeMobile() }, [location.pathname, closeMobile])
  useEffect(() => {
    if (!compact || !open) return
    const previous = document.activeElement as HTMLElement | null
    const node = navigationRef.current
    const focusable = () => [...(node?.querySelectorAll<HTMLElement>(
      'button:not(:disabled):not([tabindex="-1"]), a[href], input:not(:disabled)',
    ) ?? [])].filter((element) => element.getClientRects().length > 0)
    focusable()[0]?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close()
      if (event.key !== "Tab") return
      const items = focusable()
      const target = event.shiftKey ? items.at(-1) : items[0]
      if ((event.shiftKey && document.activeElement === items[0]) ||
          (!event.shiftKey && document.activeElement === items.at(-1))) {
        event.preventDefault()
        target?.focus()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => { window.removeEventListener("keydown", onKey); previous?.focus() }
  }, [compact, open, close])

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

  if (!open) return null

  return (
    <>
    {compact && <button type="button" aria-label={t("collapse")} onClick={close}
      className="fixed inset-0 z-40 bg-black/30" tabIndex={-1} />}
    <aside
      ref={navigationRef}
      role={compact ? "dialog" : undefined}
      aria-modal={compact ? true : undefined}
      aria-label={compact ? t("workspaceSwitcher") : undefined}
      className={cn("border-hair bg-rail flex min-h-0 flex-none flex-col border-e",
        compact ? "fixed inset-y-0 start-0 z-50 shadow-pop" : "relative")}
      style={{ width, maxWidth: compact ? "calc(100vw - 3rem)" : undefined }}
    >
      <div className="flex min-h-0 flex-1 flex-col ps-4.5 pe-3 pt-3.5 pb-2.5">
        <div className="flex items-center gap-2.5 pt-0.5 pb-4">
          <BrandMark className="min-w-0 flex-1" />
          <button
            type="button"
            className="text-n700 hover:bg-hairsoft flex size-7.5 flex-none items-center justify-center rounded-full"
            onClick={close}
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
          className="group text-ink hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base font-medium"
        >
          <span className="bg-n200 flex size-7 flex-none items-center justify-center rounded-full transition-transform duration-150 group-hover:scale-105">
            <Plus size={15} strokeWidth={2.5} />
          </span>
          {t("newProject")}
        </button>

        <div className="focus-within:bg-hairsoft hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5">
          <span className="flex size-7 flex-none items-center justify-center">
            <Search size={16} strokeWidth={2.1} className="text-ink" aria-hidden />
          </span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("search")}
            className="text-ink placeholder:text-n600 min-w-0 flex-1 bg-transparent pe-2 text-base outline-none"
          />
        </div>

        <button
          type="button"
          // Opens on the project in view, which is the one whose files the
          // person was just looking at.
          onClick={() => navigate(paths.resources(activeProject ?? undefined))}
          className="text-ink hover:bg-hairsoft mt-2.5 flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base"
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <Layers size={16} strokeWidth={2.1} />
          </span>
          {t("resourceCenter")}
        </button>

        <button
          type="button"
          // Sits right under the resource centre: the files live there, the
          // accounts they get posted from live here.
          onClick={() => navigate(paths.authCenter)}
          className="text-ink hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base"
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <KeyRound size={16} strokeWidth={2.1} />
          </span>
          {t("authCenter")}
        </button>

        <button
          type="button"
          onClick={() => navigate(paths.skills)}
          className="text-ink hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base"
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <Blocks size={16} strokeWidth={2.1} />
          </span>
          {t("skillCenter")}
        </button>

        <button
          type="button"
          onClick={() => navigate(paths.cron)}
          className="text-ink hover:bg-hairsoft mb-1.5 flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base"
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <Clock size={16} strokeWidth={2.1} />
          </span>
          {t("scheduledTasks")}
        </button>

        <button
          type="button"
          onClick={() => navigate(paths.billing())}
          aria-current={isBilling ? "page" : undefined}
          className={cn(
            "text-ink hover:bg-hairsoft mb-1.5 flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base",
            isBilling && "bg-n200 font-medium",
          )}
        >
          <span className="flex size-7 flex-none items-center justify-center">
            <CreditCard size={16} strokeWidth={2.1} />
          </span>
          {t("billing")}
        </button>

        {draftOpen && (
          <div className="border-hair mb-1 flex flex-none items-center gap-2 rounded-full border px-3.5 py-2">
            <span className="bg-accent size-1.75 rounded-full" aria-hidden />
            <input
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitDraft()
                if (e.key === "Escape") setDraftOpen(false)
              }}
              onBlur={commitDraft}
              placeholder={t("projectName")}
              className="text-ink min-w-0 flex-1 border-none bg-transparent text-base outline-none"
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
            />
          </div>
        )}

        <div className="scr -mx-1 flex min-h-0 flex-1 flex-col gap-0.5 overflow-x-hidden overflow-y-auto p-1">
          <ProjectTree
            projects={projects.data ?? []}
            sessions={filtered}
            searching={query.trim().length > 0}
          />
        </div>

        <UserRow sessionCount={(sessions.data ?? []).length} />
      </div>
      {!compact && <button
        type="button"
        aria-hidden
        tabIndex={-1}
        onMouseDown={startDrag}
        className="absolute -end-1 top-0 bottom-0 z-6 w-2 cursor-col-resize"
      />}
    </aside>
    </>
  )
}

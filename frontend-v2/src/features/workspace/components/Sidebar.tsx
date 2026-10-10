import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Link, useLocation, useNavigate } from "react-router"
import {
  Bell,
  BookOpen,
  Blocks,
  Clock,
  CreditCard,
  FolderPlus,
  KeyRound,
  Layers,
  Monitor,
  PanelLeft,
  Plus,
  Search,
  X,
} from "lucide-react"
import { useInboxUnread } from "@/shared/api/inbox"
import { cn } from "@/shared/lib/cn"
import { BrandMark } from "@/shared/ui/BrandMark"
import { paths } from "@/shared/router/paths"
import { useProjectsQuery, useCreateProject } from "../api/projects"
import { useSessionsQuery } from "../api/sessions"
import { useWorkspaceUi } from "../stores/ui"
import { AssistantEntry } from "./AssistantEntry"
import { NavTile } from "./NavTile"
import { ProjectTree } from "./ProjectTree"
import { SessionSearchResults } from "./SessionSearchResults"
import { UserRow } from "./UserRow"
import { WorkspaceSwitcher } from "./WorkspaceSwitcher"
import { useSidebarLayout } from "../hooks/useSidebarLayout"

interface SidebarProps {
  /** Passed to the user row; observation-only pages omit the (period-settling) balance read. */
  showCredits?: boolean
  assistantUnread?: { count: number; lowerBound: boolean }
}

/** Two parts. On top, where to go: the personal assistant's card, then the centre
 *  pages in one tile grid that folds to a single icon row on a short screen. Below,
 *  the work itself: new chat, new project and search sit right above the projects
 *  and their conversations they act on, the part people use most, which takes all
 *  the height left (on a laptop twelve 40px rows plus the scheduled jobs used to
 *  leave it a sliver, 2026-10-08). */
export function Sidebar({ showCredits = true, assistantUnread }: SidebarProps) {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  const width = useWorkspaceUi((s) => s.sidebarWidth)
  const { compact, open, close } = useSidebarLayout()
  const closeMobile = useWorkspaceUi((s) => s.closeMobileSidebar)
  const location = useLocation()
  const navigationRef = useRef<HTMLElement>(null)
  const setSidebarWidth = useWorkspaceUi((s) => s.setSidebarWidth)

  const projects = useProjectsQuery()
  const sessions = useSessionsQuery()
  const inboxUnread = useInboxUnread()
  const createProject = useCreateProject()
  const selectedProject = useWorkspaceUi((s) => s.selectedProject)
  // A stale selection (deleted project) must not point the resource centre
  // at a project that no longer exists.
  const activeProject =
    selectedProject && (projects.data ?? []).some((p) => p.id === selectedProject) ? selectedProject : null

  const [draftOpen, setDraftOpen] = useState(false)
  const [draftName, setDraftName] = useState("")
  const [query, setQuery] = useState("")
  const searching = query.trim().length > 0
  const listRef = useRef<HTMLDivElement>(null)
  const dragStart = useRef<{ x: number; w: number } | null>(null)

  useEffect(() => {
    closeMobile()
  }, [location.pathname, closeMobile])
  useEffect(() => {
    if (!compact || !open) return
    const previous = document.activeElement as HTMLElement | null
    const node = navigationRef.current
    const focusable = () =>
      [
        ...(node?.querySelectorAll<HTMLElement>(
          'button:not(:disabled):not([tabindex="-1"]), a[href], input:not(:disabled)',
        ) ?? []),
      ].filter((element) => element.getClientRects().length > 0)
    focusable()[0]?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close()
      if (event.key !== "Tab") return
      const items = focusable()
      const target = event.shiftKey ? items.at(-1) : items[0]
      if (
        (event.shiftKey && document.activeElement === items[0]) ||
        (!event.shiftKey && document.activeElement === items.at(-1))
      ) {
        event.preventDefault()
        target?.focus()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => {
      window.removeEventListener("keydown", onKey)
      previous?.focus()
    }
  }, [compact, open, close])

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
      {compact && (
        <button
          type="button"
          aria-label={t("collapse")}
          onClick={close}
          className="fixed inset-0 z-40 bg-black/30"
          tabIndex={-1}
        />
      )}
      <aside
        ref={navigationRef}
        role={compact ? "dialog" : undefined}
        aria-modal={compact ? true : undefined}
        aria-label={compact ? t("workspaceSwitcher") : undefined}
        className={cn(
          "border-hair bg-rail flex min-h-0 flex-none flex-col border-e",
          compact ? "shadow-pop fixed inset-y-0 start-0 z-50" : "relative",
        )}
        style={{ width, maxWidth: compact ? "calc(100vw - 3rem)" : undefined }}
      >
        <div className="flex min-h-0 flex-1 flex-col ps-4.5 pe-3 pt-3.5 pb-2.5">
          <div className="flex items-center gap-2.5 pt-0.5 pb-4 [@media(max-height:760px)]:pb-2">
            <Link to={paths.newChat()} aria-label={t("home")} className="min-w-0 flex-1">
              <BrandMark />
            </Link>
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

          <AssistantEntry unread={assistantUnread} />

          {/* The centre pages, one tile each: the everyday ones on the first row (the cloud desktop
            leads, for most people it is the one work surface they use; the message centre carries
            the cross-workspace unread total), set-up and accounts on the second. Scheduled jobs are
            listed on their own page rather than under this grid. */}
          <nav
            aria-label={t("centres")}
            className="mt-2 grid flex-none grid-cols-4 gap-0.5 [@media(max-height:760px)]:mt-1.5 [@media(max-height:760px)]:grid-cols-8"
          >
            <NavTile icon={Monitor} label={t("desktop")} to={paths.desktop} />
            <NavTile icon={Bell} label={t("inbox")} hint={t("inboxHint")} to={paths.inbox}
              badge={inboxUnread.data?.total ?? 0} />
            {/* Memories, topics and files are one place: the knowledge page. */}
            <NavTile icon={BookOpen} label={t("wiki")} to={paths.wiki()} pattern={`${paths.wiki()}/*`} />
            <NavTile icon={Clock} label={t("scheduledTasks")} to={paths.cron} pattern={`${paths.cron}/*`} />
            {/* Opens on the project in view, whose files the person was just looking at. */}
            <NavTile
              icon={Layers}
              label={t("resourceCenter")}
              hint={t("resourceCenterHint")}
              to={paths.resources(activeProject ?? undefined)}
              pattern={paths.resources()}
            />
            <NavTile icon={Blocks} label={t("skillCenter")} hint={t("skillCenterHint")} to={paths.skills} />
            <NavTile icon={KeyRound} label={t("authCenter")} hint={t("authCenterHint")} to={paths.authCenter} />
            <NavTile icon={CreditCard} label={t("billing")} to={paths.billing()} pattern={`${paths.billing()}/*`} />
          </nav>

          <section
            aria-label={t("work")}
            className="border-hair mt-2 flex min-h-0 flex-1 flex-col border-t pt-2 [@media(max-height:760px)]:mt-1.5 [@media(max-height:760px)]:pt-1.5"
          >
            {/* DEEIX-style rows: left-aligned, icon column, the primary action wears a round tinted
              icon chip instead of a filled pill. */}
            <button
              type="button"
              // Starting a conversation is the high-frequency action, so it is the
              // first row and lands in the sidebar's current project. Projects are
              // the container, created one row down.
              onClick={() => navigate(paths.newChat(activeProject ?? undefined))}
              className="group text-ink hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base font-medium [@media(max-height:760px)]:h-8.5"
            >
              <span className="bg-a200 text-n800 flex size-7 flex-none items-center justify-center rounded-full transition-transform duration-150 group-hover:scale-105">
                <Plus size={15} strokeWidth={2.5} />
              </span>
              {t("newChat")}
            </button>

            <button
              type="button"
              onClick={() => setDraftOpen(true)}
              className="text-ink hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base [@media(max-height:760px)]:h-8.5"
            >
              <span className="flex size-7 flex-none items-center justify-center">
                <FolderPlus size={16} strokeWidth={2.1} />
              </span>
              {t("newProject")}
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

            <div className="focus-within:bg-hairsoft hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 [@media(max-height:760px)]:h-8.5">
              <span className="flex size-7 flex-none items-center justify-center">
                <Search size={16} strokeWidth={2.1} className="text-ink" aria-hidden />
              </span>
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  // Enter opens the first result; Escape clears the search and brings the tree back.
                  if (e.key === "Enter") listRef.current?.querySelector<HTMLElement>("a[href]")?.click()
                  if (e.key === "Escape" && query) {
                    e.stopPropagation()
                    setQuery("")
                  }
                }}
                placeholder={t("searchPlaceholder")}
                aria-label={t("searchPlaceholder")}
                className="text-ink placeholder:text-n600 min-w-0 flex-1 bg-transparent pe-1 text-base outline-none [&::-webkit-search-cancel-button]:hidden"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery("")}
                  title={t("searchClear")}
                  aria-label={t("searchClear")}
                  className="text-n700 hover:bg-n200 me-1 flex size-6 flex-none items-center justify-center rounded-full"
                >
                  <X size={13.5} strokeWidth={2.4} />
                </button>
              )}
            </div>

            <div
              ref={listRef}
              className="scr -mx-1 mt-0.5 flex min-h-0 flex-1 flex-col gap-0.5 overflow-x-hidden overflow-y-auto p-1"
            >
              {searching ? (
                <SessionSearchResults query={query} sessions={sessions.data ?? []} projects={projects.data ?? []} />
              ) : (
                <ProjectTree projects={projects.data ?? []} sessions={sessions.data ?? []} />
              )}
            </div>
          </section>

          <UserRow sessionCount={(sessions.data ?? []).length} showCredits={showCredits} />
        </div>
        {!compact && (
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            onMouseDown={startDrag}
            className="absolute -end-1 top-0 bottom-0 z-6 w-2 cursor-col-resize"
          />
        )}
      </aside>
    </>
  )
}

import { useId, type KeyboardEvent, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import {
  ArrowRight,
  BookOpen,
  FileUp,
  MessageCircle,
  Plus,
  Search,
  SearchX,
  Sparkles,
  Upload,
  X,
} from "lucide-react"
import { paths } from "@/shared/router/paths"
import type { Project } from "@/shared/types/api"
import { KNOWLEDGE_VIEWS, type KnowledgeView } from "./data"
import { button, card, primaryButton, textButton } from "./ui"

export function SearchBar({
  value,
  onChange,
  projects,
  projectId,
  onProject,
}: {
  value: string
  onChange: (value: string) => void
  projects: Project[]
  projectId: string
  onProject: (id: string) => void
}) {
  const { t } = useTranslation("knowledge")
  return (
    <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
      <div className="border-hair bg-card focus-within:border-accent flex min-w-0 flex-1 items-center gap-2 rounded-2xl border px-3.5 transition-colors">
        <Search size={16} className="text-n500 flex-none" aria-hidden />
        <input
          type="search"
          value={value}
          maxLength={200}
          aria-label={t("searchLabel")}
          placeholder={t("searchPlaceholder")}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape" && value) onChange("")
          }}
          className="text-ink placeholder:text-n500 text-md min-w-0 flex-1 bg-transparent py-2.5 outline-none [&::-webkit-search-cancel-button]:hidden"
        />
        {value && (
          <button
            type="button"
            aria-label={t("clearSearch")}
            onClick={() => onChange("")}
            className="text-n500 hover:bg-hairsoft hover:text-ink flex size-6 flex-none items-center justify-center rounded-full"
          >
            <X size={14} />
          </button>
        )}
      </div>
      {projects.length > 0 && (
        <select
          aria-label={t("scope")}
          value={projectId}
          onChange={(event) => onProject(event.target.value)}
          className="border-hair bg-card text-ink focus:border-accent text-md min-h-11 rounded-2xl border px-3.5 outline-none sm:w-48"
        >
          <option value="">{t("allScopes")}</option>
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      )}
    </div>
  )
}

export function ViewTabs({
  view,
  counts,
  onChange,
}: {
  view: KnowledgeView
  counts: Partial<Record<KnowledgeView, string>>
  onChange: (view: KnowledgeView) => void
}) {
  const { t } = useTranslation("knowledge")
  // One Tab stop for the row; arrow keys, Home and End move between views.
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = KNOWLEDGE_VIEWS.indexOf(view)
    const last = KNOWLEDGE_VIEWS.length - 1
    const target = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: last }[event.key]
    if (target === undefined) return
    event.preventDefault()
    const next = KNOWLEDGE_VIEWS[(target + last + 1) % (last + 1)]
    onChange(next)
    event.currentTarget.querySelector<HTMLElement>(`[data-view="${next}"]`)?.focus()
  }
  return (
    <div
      role="tablist"
      aria-label={t("views")}
      className="scr -mx-1 flex gap-1 overflow-x-auto px-1 pb-1"
      onKeyDown={move}
    >
      {KNOWLEDGE_VIEWS.map((key) => {
        const active = view === key
        return (
          <button
            key={key}
            type="button"
            role="tab"
            data-view={key}
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(key)}
            className={
              "flex flex-none items-center gap-1.5 rounded-full px-3.5 py-1.5 text-sm transition-colors " +
              (active ? "bg-ink text-bg" : "text-n700 hover:bg-hairsoft hover:text-ink")
            }
          >
            {t(`view.${key}`)}
            {counts[key] && (
              <span className={"tabular-nums " + (active ? "opacity-70" : "text-n500")}>{counts[key]}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}

/** A titled block of the overview, with its way into the full list. */
export function Section({
  title,
  count,
  onSeeAll,
  children,
}: {
  title: string
  count?: string
  onSeeAll?: () => void
  children: ReactNode
}) {
  const { t } = useTranslation("knowledge")
  const id = useId()
  return (
    <section aria-labelledby={id}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 id={id} className="text-ink flex items-baseline gap-2 text-xl font-semibold tracking-tight">
          {title}
          {count && <span className="text-n500 text-sm font-normal tabular-nums">{count}</span>}
        </h2>
        {onSeeAll && (
          <button type="button" className={textButton} onClick={onSeeAll}>
            {t("seeAll")}
            <ArrowRight size={14} aria-hidden />
          </button>
        )}
      </div>
      {children}
    </section>
  )
}

/** The one-line explanation at the top of a full list. */
export function Intro({ children }: { children: ReactNode }) {
  return <p className="text-n600 mb-4 max-w-2xl text-sm leading-relaxed">{children}</p>
}

/** A section with nothing in it yet: what will appear, and how to start. */
export function Quiet({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className={card + " text-n600 flex flex-wrap items-center justify-between gap-3 px-5 py-4 text-sm"}>
      <span className="leading-relaxed">{children}</span>
      {action}
    </div>
  )
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="bg-hairsoft h-16 animate-pulse rounded-2xl" />
      ))}
    </div>
  )
}

/** The first visit: what this page will hold and the three ways it fills up. */
export function Welcome({
  projectId,
  canUpload,
  onAdd,
  onUpload,
}: {
  projectId: string
  canUpload: boolean
  onAdd: () => void
  onUpload: () => void
}) {
  const { t } = useTranslation("knowledge")
  const steps = [
    { key: "chat", icon: MessageCircle },
    { key: "topics", icon: Sparkles },
    { key: "files", icon: FileUp },
  ] as const
  return (
    <div className={card + " px-6 py-10 text-center sm:px-10 sm:py-12"}>
      <span className="bg-a100 text-a700 mx-auto flex size-12 items-center justify-center rounded-2xl">
        <BookOpen size={22} aria-hidden />
      </span>
      <h2 className="text-ink mt-4 text-2xl font-semibold tracking-tight">{t("welcome.title")}</h2>
      <p className="text-n600 text-md mx-auto mt-2 max-w-md leading-relaxed">{t("welcome.body")}</p>
      <ol className="mt-8 grid gap-3 text-start sm:grid-cols-3">
        {steps.map(({ key, icon: Icon }) => (
          <li key={key} className="bg-hairsoft/60 rounded-2xl p-4">
            <Icon size={18} className="text-a700" aria-hidden />
            <p className="text-ink text-md mt-2.5 font-medium">{t(`welcome.steps.${key}.title`)}</p>
            <p className="text-n600 mt-1 text-sm leading-relaxed">{t(`welcome.steps.${key}.body`)}</p>
          </li>
        ))}
      </ol>
      <div className="mt-8 flex flex-wrap justify-center gap-2">
        <Link className={primaryButton} to={paths.newChat(projectId || undefined)}>
          <MessageCircle size={15} aria-hidden />
          {t("welcome.startChat")}
        </Link>
        <button type="button" className={button} onClick={onAdd}>
          <Plus size={15} aria-hidden />
          {t("addMemory")}
        </button>
        <button type="button" className={button} disabled={!canUpload} onClick={onUpload}>
          <Upload size={15} aria-hidden />
          {t("uploadFile")}
        </button>
      </div>
    </div>
  )
}

export function NoResults({ query, projectId }: { query: string; projectId: string }) {
  const { t } = useTranslation("knowledge")
  return (
    <div className="flex flex-col items-center px-6 py-16 text-center">
      <span className="bg-hairsoft text-n600 flex size-12 items-center justify-center rounded-2xl">
        <SearchX size={22} aria-hidden />
      </span>
      <p className="text-ink mt-4 text-lg font-medium break-all">{t("empty.searchTitle", { query })}</p>
      <p className="text-n600 mt-1.5 max-w-sm text-sm leading-relaxed">{t("empty.searchBody")}</p>
      <Link className={button + " mt-5"} to={paths.newChat(projectId || undefined)}>
        <MessageCircle size={15} aria-hidden />
        {t("empty.askAssistant")}
      </Link>
    </div>
  )
}

import { Fragment, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { Link, useMatch } from "react-router"
import { MessageSquare, Sparkles } from "lucide-react"
import type { Project, Session } from "@/shared/types/api"
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue"
import { cn } from "@/shared/lib/cn"
import { paths, routePatterns } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { useSessionSearch, type SessionSearchHit } from "../api/sessions"
import { splitMatches } from "../lib/splitMatches"
import { useWorkspaceUi } from "../stores/ui"

interface SessionSearchResultsProps {
  query: string
  sessions: Session[]
  projects: Project[]
}

const SETTLE_MS = 250

/** Titles in the list already loaded match at once; the server adds what was said in the conversations. */
function titleMatches(sessions: Session[], words: string): SessionSearchHit[] {
  const needle = words.toLowerCase()
  return sessions
    .filter((s) => s.kind !== "cron" && s.title.toLowerCase().includes(needle))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .map((s) => ({
      session_id: s.id,
      title: s.title,
      project_id: s.project_id ?? null,
      kind: s.kind ?? "normal",
      match: "title",
      snippet: "",
      role: null,
      time: s.updated_at,
    }))
}

function Marked({ text, words }: { text: string; words: string }) {
  return (
    <>
      {splitMatches(text, words).map((piece, index) =>
        piece.match ? (
          // Matches are set in the sidebar's ink, bolder: no second colour beside the neutral list.
          <mark key={index} className="text-ink bg-transparent font-semibold">
            {piece.text}
          </mark>
        ) : (
          <Fragment key={index}>{piece.text}</Fragment>
        ),
      )}
    </>
  )
}

/** The sidebar search: conversations whose title or messages contain the query, in place of the
 *  project tree while there is a query. */
export function SessionSearchResults({ query, sessions, projects }: SessionSearchResultsProps) {
  const { t } = useTranslation("workspace")
  const words = query.trim().replace(/\s+/g, " ")
  const settled = useDebouncedValue(words, SETTLE_MS)
  const search = useSessionSearch(settled)
  const activeSessionId = useMatch(`${paths.app}/${routePatterns.chat}`)?.params.sessionId
  const onAssistant = useMatch(paths.assistant) !== null
  const selectProject = useWorkspaceUi((s) => s.selectProject)
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name])), [projects])
  const current = settled === words
  const fresh = current && search.data !== undefined && !search.isPlaceholderData
  const failed = current && search.isError
  const hits = useMemo(
    () => (fresh && search.data ? search.data : titleMatches(sessions, words)),
    [fresh, search.data, sessions, words],
  )

  return (
    <ul aria-label={t("searchResults")} className="flex flex-col gap-0.5">
      {hits.map((hit) => {
        const assistant = hit.kind === "assistant"
        const Icon = assistant ? Sparkles : MessageSquare
        const project = assistant ? "" : (names.get(hit.project_id ?? "") ?? "")
        const active = assistant ? onAssistant : hit.session_id === activeSessionId
        return (
          <li key={hit.session_id}>
            <Link
              to={assistant ? paths.assistant : paths.chat(hit.session_id)}
              // Opening a chat makes its project the current one for "new chat", as in the tree.
              onClick={assistant ? undefined : () => selectProject(hit.project_id)}
              aria-current={active ? "page" : undefined}
              className={cn(
                "hover:bg-hairsoft flex flex-col gap-0.5 rounded-xl px-2.5 py-1.5",
                active && "bg-n200",
              )}
            >
              <span className="text-ink flex min-w-0 items-center gap-1.5 text-base">
                <Icon size={13.5} strokeWidth={2.2} className="text-n600 flex-none" aria-hidden />
                <span className={cn("min-w-0 flex-1 truncate", active && "font-medium")}>
                  <Marked text={assistant ? t("assistant") : hit.title || t("untitledChat")} words={words} />
                </span>
              </span>
              {(project || hit.snippet) && (
                <span className="text-n600 line-clamp-2 ps-5 text-xs leading-4 break-all">
                  {project && <span className="text-n700">{project}</span>}
                  {project && hit.snippet && " · "}
                  {hit.snippet && (
                    <>
                      {hit.role === "user" && t("searchYou")}
                      <Marked text={hit.snippet} words={words} />
                    </>
                  )}
                </span>
              )}
            </Link>
          </li>
        )
      })}
      {!fresh && !failed && (
        <li className="text-n600 flex items-center gap-2 px-2.5 py-1.5 text-sm" aria-live="polite">
          <Spinner className="size-3 flex-none" />
          {t("searching")}
        </li>
      )}
      {failed && <li className="text-n600 px-2.5 py-1.5 text-sm">{t("searchFailed")}</li>}
      {fresh && hits.length === 0 && (
        <li className="text-n600 px-2.5 py-2 text-sm" aria-live="polite">
          {t("searchEmpty")}
        </li>
      )}
    </ul>
  )
}

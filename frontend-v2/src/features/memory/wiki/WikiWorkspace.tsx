import { useState } from "react"
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"
import { useTranslation } from "react-i18next"
import { BookOpen, Plus, RefreshCw, Search } from "lucide-react"
import { paths } from "@/shared/router/paths"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryProjects, useMemoryScope } from "../api"
import { wikiApi } from "../wiki-api"
import { WikiEditor } from "./WikiEditor"
import { WikiReader } from "./WikiReader"
import { WikiUpload } from "./WikiUpload"

export function WikiWorkspace() {
  const { userId, workspaceId } = useMemoryScope()
  const [params] = useSearchParams()
  return <WikiLibrary key={[userId, workspaceId, params.get("project")].join(":")} />
}

function WikiLibrary() {
  const { t } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const projects = useMemoryProjects()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { pageId } = useParams()
  const [params] = useSearchParams()
  const projectId = params.get("project") ?? ""
  const [search, setSearch] = useState(params.get("q") ?? "")
  const [query, setQuery] = useState(search)
  const [editor, setEditor] = useState<string | null>(null)
  const [notice, setNotice] = useState("")
  const capability = useQuery({ queryKey: [...key, "wiki-capabilities"], queryFn: wikiApi.capabilities })
  const library = useInfiniteQuery({
    queryKey: [...key, "wiki-library", projectId, query],
    queryFn: ({ pageParam }) => wikiApi.library({ projectId, query, status: "all", offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 10000,
  })
  const pages = library.data?.pages.flatMap((batch) => batch.pages) ?? []
  const error = library.error ?? projects.error ?? capability.error
  const saved = () => {
    setEditor(null)
    setNotice(t("consumer.saved"))
    void qc.invalidateQueries({ queryKey: key })
  }
  // Legacy management URLs show the consumer library. Reading never starts jobs.
  return (
    <div className="scr h-full min-w-0 overflow-auto">
      <main className="mx-auto max-w-6xl space-y-6 p-4 sm:p-7">
        {!pageId && (
          <header className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h1 className="text-3xl font-semibold tracking-tight">{t("title")}</h1>
              <p className="text-n600 mt-2 max-w-2xl text-sm leading-6">{t("consumer.subtitle")}</p>
            </div>
            <button
              className={memoryPrimary + " inline-flex items-center gap-2"}
              disabled={!capability.data?.enabled}
              onClick={() => {
                setNotice("")
                setEditor("new")
              }}
            >
              <Plus className="size-4" />
              {t("consumer.add")}
            </button>
          </header>
        )}
        {notice && (
          <p role="status" className="bg-a100 text-a700 rounded-xl p-4 text-sm">
            {notice}
          </p>
        )}
        {error && (
          <div role="alert" className="border-danger text-danger rounded-xl border p-4 text-sm">
            {errorText(error)}{" "}
            <button className={memoryButton} onClick={() => void qc.invalidateQueries({ queryKey: key })}>
              {t("retry")}
            </button>
          </div>
        )}
        {editor !== null ? (
          <WikiEditor
            key={editor}
            pageId={editor === "new" ? undefined : editor}
            projectId={projectId}
            onSaved={saved}
            onClose={() => setEditor(null)}
          />
        ) : pageId ? (
          <WikiReader
            key={pageId}
            pageId={pageId}
            pages={library.error ? [] : pages}
            projectId={projectId}
            onEdit={(page) => {
              setNotice("")
              setEditor(page.id)
            }}
          />
        ) : (
          <>
            <WikiUpload key={projectId} projectId={projectId} enabled={Boolean(capability.data?.enabled)} />
            <form
              className="flex flex-wrap items-end gap-3"
              onSubmit={(event) => {
                event.preventDefault()
                setQuery(search.trim())
              }}
            >
              <div className="relative min-w-44 flex-1">
                <Search className="text-n400 pointer-events-none absolute start-3 top-3 size-4" />
                <input
                  aria-label={t("searchPages")}
                  className={memoryInput + " w-full ps-9"}
                  placeholder={t("searchPlaceholder")}
                  value={search}
                  maxLength={200}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
              <button type="submit" className={memoryButton}>
                {t("search")}
              </button>
              <select
                aria-label={t("scope")}
                className={memoryInput}
                value={projectId}
                onChange={(event) => void navigate(paths.wiki(event.target.value))}
              >
                <option value="">{t("allProjects")}</option>
                {projects.data?.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                aria-label={t("refresh")}
                className={memoryButton}
                onClick={() => void library.refetch()}
              >
                <RefreshCw className="size-4" />
              </button>
            </form>
            {library.isPending && (
              <p role="status" className="p-8 text-center">
                {t("loading")}
              </p>
            )}
            {!library.isPending && !error && !pages.length && (
              <div className="border-hair bg-card rounded-2xl border px-6 py-14 text-center">
                <BookOpen className="text-n400 mx-auto mb-4 size-9" />
                <h2 className="mb-2 text-lg font-medium">{t(query ? "noMatches" : "consumer.emptyTitle")}</h2>
                <p className="text-n500 mx-auto max-w-md text-sm leading-6">
                  {t(query ? "noMatchesHint" : "consumer.emptyHint")}
                </p>
              </div>
            )}
            {!library.error && (
              <div className="grid gap-4 sm:grid-cols-2">
                {pages.map((page) => (
                  <Link
                    key={page.id}
                    to={paths.wikiPage(page.id, projectId)}
                    className="border-hair bg-card hover:border-accent group flex min-w-0 flex-col gap-4 rounded-2xl border p-5 transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <BookOpen className="text-a700 size-5" />
                      {!page.body_available && (
                        <span className="text-n500 text-xs">{t("consumer.updating")}</span>
                      )}
                    </div>
                    <div>
                      <h2 className="group-hover:text-a700 text-lg leading-7 font-semibold break-words">
                        {page.title}
                      </h2>
                      <p className="text-n600 mt-2 line-clamp-3 text-sm leading-6 break-words">
                        {page.body_available ? page.excerpt.replace(/[#*`]/g, "") : t("consumer.staleHint")}
                      </p>
                    </div>
                    <div className="text-n500 mt-auto flex flex-wrap gap-3 text-xs">
                      <span>{t("sourceCount", { count: page.source_count })}</span>
                      <span className="ms-auto">{t("readPage")} →</span>
                    </div>
                  </Link>
                ))}
              </div>
            )}
            {library.hasNextPage && (
              <button
                className={memoryButton}
                disabled={library.isFetchingNextPage}
                onClick={() => void library.fetchNextPage()}
              >
                {t("loadMore")}
              </button>
            )}
            <p className="text-n500 text-xs leading-6">
              {t("consumer.editHint")}{" "}
              <Link className="text-a700 hover:underline" to={paths.memory}>
                {t("consumer.memoryLink")}
              </Link>
            </p>
          </>
        )}
      </main>
    </div>
  )
}

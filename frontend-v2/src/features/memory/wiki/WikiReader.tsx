import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ArrowLeft, Download, FileDown, FileText, Loader, Pencil } from "lucide-react"
import { ApiError } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { formatDateTime } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { useMemoryScope } from "../api"
import { useBackState, useBackTarget } from "../knowledge/back"
import { button } from "../knowledge/ui"
import { wikiApi, type WikiPage, type WikiSummary } from "../wiki-api"
import { relatedPages, wikiExport } from "./content"
import { WikiMarkdown, WikiOutline } from "./WikiMarkdown"
import { WikiEvidence } from "./WikiEvidence"
import { documentsApi } from "./documents-api"

/** One topic or document page, laid out for reading: the text in a single
 *  column, its outline and sources beside it on wide screens. */
export function WikiReader({
  pageId,
  pages,
  projectId,
  onEdit,
}: {
  pageId: string
  pages: WikiSummary[]
  projectId: string
  onEdit: (page: WikiPage) => void
}) {
  const { t } = useTranslation(["wiki", "knowledge"])
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const back = useBackState()
  const backTo = useBackTarget(paths.wiki(projectId))
  const [active, setActive] = useState<number | null>(null)
  const [exporting, setExporting] = useState(false)
  const [downloadError, setDownloadError] = useState<unknown>(null)
  const query = useQuery({
    queryKey: [...key, "wiki-page", pageId],
    queryFn: () => wikiApi.page(pageId),
    gcTime: 0,
    refetchOnMount: "always",
    // A page that is gone stays gone: no retries, no polling.
    retry: (count, error) => !isMissing(error) && count < 1,
    refetchInterval: (current) => (isMissing(current.state.error) ? false : 10000),
  })
  const page = query.data
  const selectCitation = (index: number | null) => {
    setActive(index)
    if (index === null) return
    document.getElementById("wiki-citation-" + index)?.scrollIntoView({ block: "nearest", behavior: "auto" })
  }
  const download = async () => {
    setExporting(true)
    try {
      const result = await query.refetch()
      if (result.error || !result.data?.body_available) return
      const url = URL.createObjectURL(
        new Blob([wikiExport(result.data)], { type: "text/markdown;charset=utf-8" }),
      )
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = result.data.slug + ".md"
      anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } finally {
      setExporting(false)
    }
  }
  const backLink = (
    <Link
      className="text-n600 hover:text-ink inline-flex items-center gap-1.5 text-sm transition-colors"
      to={backTo}
    >
      <ArrowLeft className="size-4" />
      {t("title", { ns: "knowledge" })}
    </Link>
  )
  if (query.isPending)
    return (
      <div className="mx-auto max-w-5xl px-4 py-8 sm:px-8">
        {backLink}
        <p role="status" className="text-n600 mt-10 flex items-center gap-2 text-sm">
          <Spinner />
          {t("loading")}
        </p>
      </div>
    )
  if (isMissing(query.error))
    return (
      <div className="mx-auto max-w-5xl px-4 py-8 sm:px-8">
        {backLink}
        <div className="mt-10 max-w-xl">
          <h1 className="text-ink text-xl font-semibold">{t("consumer.missingTitle")}</h1>
          <p className="text-n600 mt-2 text-sm leading-relaxed">{t("consumer.missingHint")}</p>
          <Link className={button + " mt-5"} to={backTo}>
            {t("consumer.missingBack")}
          </Link>
        </div>
      </div>
    )
  if (query.error || !page)
    return (
      <div className="mx-auto max-w-5xl space-y-4 px-4 py-8 sm:px-8">
        {backLink}
        <p role="alert" className="bg-dangersoft text-dangerink rounded-xl px-4 py-3 text-sm">
          {errorText(query.error)}
        </p>
        <button className={button} onClick={() => void query.refetch()}>
          {t("retry")}
        </button>
      </div>
    )
  const related = relatedPages(page, pages)
  // Retired: its facts live on in memories or a merged topic; nothing to edit here.
  const retired = page.status === "retired"
  return (
    <article className="mx-auto max-w-5xl px-4 pt-6 pb-16 sm:px-8 sm:pt-8" aria-label={page.title}>
      {backLink}
      <header className="border-hair mt-5 border-b pb-6">
        <h1 className="text-ink text-3xl leading-tight font-semibold tracking-tight break-words sm:text-4xl">
          {page.title}
        </h1>
        <div className="text-n600 mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          {page.document && (
            <span className="inline-flex items-center gap-1.5">
              <FileText className="size-3.5" aria-hidden />
              {page.document.filename}
            </span>
          )}
          {!page.body_available && !retired && (
            <span className="inline-flex items-center gap-1.5">
              <Loader className="size-3.5" aria-hidden />
              {t("consumer.updating")}
            </span>
          )}
          {page.updated_at && <time dateTime={page.updated_at}>{formatDateTime(page.updated_at)}</time>}
        </div>
        {!retired && (
          <div className="mt-5 flex flex-wrap gap-2">
            <button className={button} onClick={() => onEdit(page)}>
              <Pencil className="size-3.5" aria-hidden />
              {t("consumer.edit")}
            </button>
            <button
              className={button}
              disabled={!page.body_available || exporting}
              onClick={() => void download()}
            >
              <FileDown className="size-3.5" aria-hidden />
              {t("export")}
            </button>
            {page.document && (
              <button
                className={button}
                onClick={() => {
                  setDownloadError(null)
                  void documentsApi.download(page.document!.id).catch(setDownloadError)
                }}
              >
                <Download className="size-3.5" aria-hidden />
                {t("documents.download")}
              </button>
            )}
          </div>
        )}
        {downloadError !== null && (
          <p role="alert" className="text-dangerink mt-3 text-sm">
            {errorText(downloadError)}
          </p>
        )}
        {page.document && page.document.sections.length > 1 && (
          <nav aria-label={t("documents.sections")} className="mt-5 flex flex-wrap gap-2">
            {page.document.sections.map((section) => (
              <Link
                key={section.id}
                aria-current={section.id === page.id ? "page" : undefined}
                to={paths.wikiPage(section.id, projectId)}
                state={back}
                className={
                  "rounded-full border px-3 py-1 text-sm transition-colors " +
                  (section.id === page.id
                    ? "border-ink bg-ink text-bg"
                    : "border-hair text-n700 hover:bg-hairsoft hover:text-ink")
                }
              >
                {section.title}
              </Link>
            ))}
          </nav>
        )}
      </header>
      {!page.body_available ? (
        <div className="bg-hairsoft/60 mt-8 max-w-2xl space-y-2 rounded-2xl px-6 py-6">
          <h2 className="text-ink text-lg font-medium">
            {t(retired ? "consumer.retiredTitle" : "consumer.updating")}
          </h2>
          <p className="text-n600 text-sm leading-relaxed">
            {t(retired ? "consumer.retiredHint" : "consumer.staleHint")}
          </p>
        </div>
      ) : (
        <div className="mt-8 grid min-w-0 gap-10 xl:grid-cols-[minmax(0,1fr)_15rem]">
          <div className="min-w-0">
            <WikiMarkdown
              page={{ ...page, body: page.body?.replace(/^# [^\n]*\n+/, "") ?? null }}
              pages={pages}
              onCitation={selectCitation}
            />
            {related.length > 0 && (
              <section className="border-hair mt-12 border-t pt-6">
                <h2 className="text-ink text-sm font-semibold">{t("related")}</h2>
                <p className="text-n600 mt-1 mb-3 text-xs">{t("relatedHint")}</p>
                <div className="flex flex-wrap gap-2">
                  {related.map((other) => (
                    <Link
                      key={other.id}
                      className={button}
                      to={paths.wikiPage(other.id, projectId)}
                      state={back}
                    >
                      {other.title}
                    </Link>
                  ))}
                </div>
              </section>
            )}
          </div>
          <aside className="border-hair scr min-w-0 border-t pt-6 xl:sticky xl:top-6 xl:max-h-[calc(100dvh-9rem)] xl:self-start xl:overflow-y-auto xl:border-t-0 xl:pt-0">
            {/* A table of contents helps beside the text, not after it. */}
            <div className="hidden xl:block">
              <WikiOutline body={page.body} />
            </div>
            <WikiEvidence page={page} active={active} onSelect={selectCitation} />
          </aside>
        </div>
      )}
    </article>
  )
}

function isMissing(error: unknown) {
  return error instanceof ApiError && error.status === 404
}

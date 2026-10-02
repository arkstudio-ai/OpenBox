import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ArrowLeft, Download, Pencil, BookOpen } from "lucide-react"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import { memoryButton } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { wikiApi, type WikiPage, type WikiSummary } from "../wiki-api"
import { relatedPages, wikiExport } from "./content"
import { WikiMarkdown, WikiOutline } from "./WikiMarkdown"
import { WikiEvidence } from "./WikiEvidence"
import { documentsApi } from "./documents-api"

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
  const { t, i18n } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const [active, setActive] = useState<number | null>(null)
  const [exporting, setExporting] = useState(false)
  const [downloadError, setDownloadError] = useState<unknown>(null)
  const query = useQuery({
    queryKey: [...key, "wiki-page", pageId],
    queryFn: () => wikiApi.page(pageId),
    gcTime: 0,
    refetchOnMount: "always",
    refetchInterval: 10000,
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
  if (query.isPending)
    return (
      <p role="status" className="p-8">
        {t("loading")}
      </p>
    )
  if (query.error || !page)
    return (
      <div className="space-y-4 p-8">
        <p role="alert" className="text-danger">
          {errorText(query.error)}
        </p>
        <button className={memoryButton} onClick={() => void query.refetch()}>
          {t("retry")}
        </button>
      </div>
    )
  const related = relatedPages(page, pages)
  const dateFormat = new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeStyle: "short" })
  return (
    <article className="border-hair bg-card min-w-0 rounded-2xl border" aria-label={page.title}>
      <header className="border-hair space-y-5 border-b p-5 sm:p-7">
        <Link
          className="text-n500 inline-flex items-center gap-1.5 text-xs hover:underline"
          to={paths.wiki(projectId)}
        >
          <ArrowLeft className="size-3.5" />
          {t("allPages")}
        </Link>
        <div className="flex items-start gap-3">
          <BookOpen className="text-a700 mt-1 size-6 shrink-0" />
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl leading-tight font-semibold tracking-tight break-words sm:text-3xl">
              {page.title}
            </h1>
            <div className="text-n500 mt-3 flex flex-wrap items-center gap-2.5 text-xs">
              {!page.body_available && <span>{t("consumer.updating")}</span>}
              {page.updated_at && (
                <time dateTime={page.updated_at}>{dateFormat.format(new Date(page.updated_at))}</time>
              )}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {page.document && (
            <button
              className={memoryButton}
              onClick={() => {
                setDownloadError(null)
                void documentsApi.download(page.document!.id).catch(setDownloadError)
              }}
            >
              {t("documents.download")}
            </button>
          )}
          <button className={memoryButton + " inline-flex items-center gap-2"} onClick={() => onEdit(page)}>
            <Pencil className="size-3.5" />
            {t("consumer.edit")}
          </button>
          <button
            className={memoryButton + " inline-flex items-center gap-2"}
            disabled={!page.body_available || exporting}
            onClick={() => void download()}
          >
            <Download className="size-3.5" />
            {t("export")}
          </button>
        </div>
        {downloadError !== null && (
          <p role="alert" className="text-danger text-sm">
            {errorText(downloadError)}
          </p>
        )}
        {page.document && page.document.sections.length > 1 && (
          <nav aria-label={t("documents.sections")} className="flex flex-wrap gap-2">
            {page.document.sections.map((section) => (
              <Link
                key={section.id}
                className={memoryButton}
                aria-current={section.id === page.id ? "page" : undefined}
                to={paths.wikiPage(section.id, projectId)}
              >
                {section.title}
              </Link>
            ))}
          </nav>
        )}
      </header>
      {!page.body_available ? (
        <div className="space-y-4 p-7">
          <h2 className="text-lg font-medium">{t("consumer.updating")}</h2>
          <p className="text-n600 max-w-xl text-sm leading-7">{t("consumer.staleHint")}</p>
        </div>
      ) : (
        <div className="grid min-w-0 gap-7 p-5 sm:p-7 xl:grid-cols-[minmax(0,1fr)_224px]">
          <div className="min-w-0">
            <WikiMarkdown
              page={{ ...page, body: page.body?.replace(/^# [^\n]*\n+/, "") ?? null }}
              pages={pages}
              onCitation={selectCitation}
            />
            <section className="border-hair mt-10 border-t pt-6">
              <h3 className="text-sm font-semibold">{t("related")}</h3>
              <p className="text-n500 mt-1 mb-3 text-xs">{t("relatedHint")}</p>
              {related.length ? (
                <div className="flex flex-wrap gap-2">
                  {related.map((other) => (
                    <Link key={other.id} className={memoryButton} to={paths.wikiPage(other.id, projectId)}>
                      {other.title}
                    </Link>
                  ))}
                </div>
              ) : (
                <p className="text-n500 text-sm">{t("noRelated")}</p>
              )}
            </section>
          </div>
          <aside className="border-hair min-w-0 border-t pt-6 xl:border-s xl:border-t-0 xl:ps-6 xl:pt-0">
            <WikiOutline body={page.body} />
            <WikiEvidence page={page} active={active} onSelect={selectCitation} />
          </aside>
        </div>
      )}
    </article>
  )
}

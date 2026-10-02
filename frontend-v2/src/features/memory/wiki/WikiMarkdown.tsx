import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import type { WikiCandidate, WikiPage, WikiSummary } from "../wiki-api"
import { citationsOf } from "./content"
import { exchangeHref, remarkOutline, remarkWiki } from "./markdown"

export function WikiMarkdown({
  page,
  pages = [],
  onCitation,
}: {
  page: WikiPage | WikiCandidate
  pages?: WikiSummary[]
  onCitation?: (index: number) => void
}) {
  const { t } = useTranslation("wiki")
  if (!page.body_available) return null
  const citations = citationsOf(page)
  return (
    <div className="wiki-prose min-w-0 text-[15px] leading-7 break-words">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm, [remarkWiki, { citations, pages, projectId: page.project_id }]]}
        components={{
          h1: ({ children, id }) => (
            <h2 id={id} className="mt-2 mb-6 text-2xl font-semibold tracking-tight">
              {children}
            </h2>
          ),
          h2: ({ children, id }) => (
            <h2 id={id} className="mt-9 mb-3 scroll-mt-6 text-xl font-semibold tracking-tight">
              {children}
            </h2>
          ),
          h3: ({ children, id }) => (
            <h3 id={id} className="mt-6 mb-3 scroll-mt-6 text-lg font-medium">
              {children}
            </h3>
          ),
          p: ({ children }) => <p className="mb-4 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="mb-4 list-disc space-y-1.5 ps-5">{children}</ul>,
          ol: ({ children }) => <ol className="mb-4 list-decimal space-y-1.5 ps-5">{children}</ol>,
          blockquote: ({ children }) => (
            <blockquote className="border-accent text-n700 my-4 border-s-2 ps-4">{children}</blockquote>
          ),
          pre: ({ children }) => (
            <pre className="bg-rail my-4 overflow-x-auto rounded-xl p-4 text-sm">{children}</pre>
          ),
          code: ({ children }) => (
            <code className="bg-rail rounded px-1 py-0.5 text-[0.9em]">{children}</code>
          ),
          table: ({ children }) => (
            <div className="border-hair my-5 overflow-x-auto rounded-lg border">
              <table className="w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="bg-rail border-hair border-b px-3 py-2 text-start font-medium">{children}</th>
          ),
          td: ({ children }) => <td className="border-hair border-b px-3 py-2">{children}</td>,
          img: ({ alt }) => <span className="text-n500">{alt}</span>,
          a: ({ href, children }) => {
            href = exchangeHref(href, page)
            if (!href)
              return (
                <span className="text-n500" title={t("unavailableLink")}>
                  {children}
                </span>
              )
            if (href.startsWith("#wiki-import-source-"))
              return (
                <a className="text-a700 underline" href={href}>
                  {children}
                </a>
              )
            if (href?.startsWith("#wiki-citation-")) {
              const index = Number(href.slice("#wiki-citation-".length))
              return (
                <button
                  type="button"
                  onClick={() => onCitation?.(index)}
                  aria-label={t("citationNumber", { number: index + 1 })}
                  className="bg-a100 text-a700 mx-1 inline-flex min-h-6 min-w-6 items-center justify-center rounded-md px-1.5 align-baseline text-xs font-medium hover:underline"
                >
                  {children}
                </button>
              )
            }
            if (href?.startsWith("#wiki-page-"))
              return (
                <Link
                  className="text-a700 underline underline-offset-4"
                  to={paths.wikiPage(href.slice("#wiki-page-".length), page.project_id ?? "")}
                >
                  {children}
                </Link>
              )
            return (
              <a
                className="text-a700 underline underline-offset-4"
                href={href}
                target="_blank"
                rel="noopener noreferrer"
              >
                {children}
              </a>
            )
          },
        }}
      >
        {page.body?.replace(/^# [^\n]*\n+/, "") ?? ""}
      </ReactMarkdown>
    </div>
  )
}

export function WikiOutline({ body }: { body: string | null }) {
  const { t } = useTranslation("wiki")
  if (!body || !/^#{2,6} /m.test(body)) return null
  return (
    <nav aria-label={t("outline")} className="border-hair mb-6 space-y-2 border-b pb-5">
      <h3 className="mb-3 text-sm font-semibold">{t("outline")}</h3>
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkOutline]}
        components={{
          h2: ({ children, id }) => (
            <a
              className="text-n600 hover:text-a700 block text-xs leading-6"
              href={"#" + encodeURIComponent(id ?? "")}
            >
              {children}
            </a>
          ),
          h3: ({ children, id }) => (
            <a
              className="text-n500 hover:text-a700 block ps-2 text-xs leading-6"
              href={"#" + encodeURIComponent(id ?? "")}
            >
              {children}
            </a>
          ),
          img: () => null,
          a: ({ children }) => <span>{children}</span>,
        }}
      >
        {body.replace(/^# [^\n]*\n+/, "")}
      </ReactMarkdown>
    </nav>
  )
}

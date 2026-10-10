import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
import type { WikiCandidate, WikiPage } from "../wiki-api"
import { citationsOf } from "./content"

export function WikiEvidence({
  page,
  active,
  onSelect,
}: {
  page: WikiPage | WikiCandidate
  active: number | null
  onSelect: (index: number | null) => void
}) {
  const { t } = useTranslation("wiki")
  const citations = citationsOf(page)
  return (
    <section id="wiki-evidence" className="min-w-0 scroll-mt-6" aria-label={t("evidence")}>
      <h3 className="mb-1 text-sm font-semibold">{t("evidence")}</h3>
      <p className="text-n500 mb-4 text-xs leading-5">{t("evidenceHint")}</p>
      <div className="space-y-3">
        {citations.map((citation, index) => {
          const source =
            "source_details" in page
              ? page.source_details?.find((source) => source.id === citation.sourceId)
              : undefined
          return (
            <div
              key={citation.sourceId + ":" + index}
              id={"wiki-citation-" + index}
              className={
                (active === index ? "border-accent bg-a100/30 " : "border-hair bg-rail/40 ") +
                "scroll-mt-5 rounded-xl border p-3 text-sm"
              }
            >
              <button
                type="button"
                aria-expanded={active === index}
                className="text-a700 mb-2 flex w-full items-center gap-2 text-start text-xs font-medium"
                onClick={() => onSelect(active === index ? null : index)}
              >
                <span className="bg-card border-hair flex size-6 shrink-0 items-center justify-center rounded-md border">
                  {index + 1}
                </span>
                {source?.kind === "verified_memory_revision"
                  ? t("correctedSource")
                  : source?.edited
                    ? t("documents.editedSource")
                    : // Same wording as a memory's sources: where the words came from.
                      t(`sourceFrom.${source?.kind}`, { defaultValue: t("sourceRevision") })}
              </button>
              {citation.quotes.map((quote) => (
                <blockquote
                  key={quote}
                  className="text-n700 mb-2 text-xs leading-6 break-words whitespace-pre-wrap"
                >
                  {quote}
                </blockquote>
              ))}
              {source?.filename && (
                <p className="text-n500 mb-2 text-xs break-words">
                  {source.filename}
                  {source.original_pages?.length
                    ? " · " + t("documents.originalPages", { pages: source.original_pages.join(", ") })
                    : ""}
                </p>
              )}
              {active === index && source && (
                <div className="border-hair mt-3 space-y-3 border-t pt-3">
                  <details>
                    <summary className="cursor-pointer text-xs font-medium">
                      {t(source.edited ? "documents.currentText" : "sourceOriginal")}
                    </summary>
                    <p className="text-n600 mt-2 max-h-72 overflow-auto text-xs leading-6 break-words whitespace-pre-wrap">
                      {source.body}
                    </p>
                  </details>
                  {source.session_id && (
                    <Link className="text-a700 block text-xs underline" to={paths.chat(source.session_id)}>
                      {t("sourceConversation")}
                    </Link>
                  )}
                  {source.changes?.map((change, changeIndex) => (
                    <div key={changeIndex} className="space-y-2 text-xs leading-6">
                      <p className="font-medium">{t("correctionOriginal")}</p>
                      <blockquote className="break-words whitespace-pre-wrap">{change.body}</blockquote>
                      {change.session_id && (
                        <Link className="text-a700 underline" to={paths.chat(change.session_id)}>
                          {t("sourceConversation")}
                        </Link>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
      {!citations.length && <p className="text-n500 text-sm">{t("noEvidence")}</p>}
      {"source_details" in page &&
        page.source_details
          ?.filter((source) => source.kind === "wiki_import")
          .map((source) => (
            <details
              key={source.id}
              id={"wiki-import-source-" + source.id}
              className="border-hair mt-3 scroll-mt-6 rounded-lg border p-3"
            >
              <summary className="cursor-pointer text-xs break-all">
                {t("importSource")} · {source.path}
              </summary>
              <p className="text-n600 mt-2 max-h-72 overflow-auto text-xs leading-6 break-words whitespace-pre-wrap">
                {source.body}
              </p>
            </details>
          ))}
    </section>
  )
}

import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { MemoryStatus, memoryButton, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { wikiApi, type WikiCandidate, type WikiPage, type WikiSummary } from "../wiki-api"
import { WikiMarkdown } from "./WikiMarkdown"
import { WikiEvidence } from "./WikiEvidence"

export function WikiReviews({
  candidates,
  pages,
  enabled,
  onPublished,
}: {
  candidates: WikiCandidate[]
  pages: WikiSummary[]
  enabled: boolean
  onPublished: (page: WikiPage) => void
}) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const errorText = useApiErrorMessage()
  const [selected, setSelected] = useState<string | null>(null)
  const [history, setHistory] = useState(false)
  const visible = candidates.filter((candidate) => history || candidate.status === "pending")
  const candidate = visible.find((item) => item.id === selected) ?? visible[0]
  const [activeCitation, setActiveCitation] = useState<number | null>(null)
  const current = pages.find(
    (page) => page.slug === candidate?.slug && page.project_id === candidate?.project_id,
  )
  const previous = useQuery({
    queryKey: [...key, "wiki-page", current?.id],
    queryFn: () => wikiApi.page(current!.id),
    enabled: !!current && !!candidate?.expected_target_revision,
    gcTime: 0,
    refetchInterval: 10000,
  })
  const decision = useMutation({
    mutationFn: async (action: "approve" | "reject") => {
      if (!candidate) throw new Error(t("noReview"))
      if (action === "approve") return { page: await wikiApi.approve(candidate) }
      await wikiApi.reject(candidate)
      return { page: null }
    },
    onSuccess: (result) => {
      void qc.invalidateQueries({ queryKey: key })
      if (result.page) onPublished(result.page)
    },
  })
  return (
    <section className="space-y-4" aria-label={t("reviews")}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">{t("reviews")}</h2>
          <p className="text-n500 mt-1 text-sm">{t("reviewHint")}</p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={history} onChange={(event) => setHistory(event.target.checked)} />
          {t("showHistory")}
        </label>
      </div>
      {!visible.length && (
        <div className="border-hair bg-card rounded-xl border p-8 text-center">
          <p className="text-n600">{t("noReview")}</p>
        </div>
      )}
      {!!visible.length && (
        <div className="flex flex-wrap gap-2">
          {visible.map((item) => (
            <button
              key={item.id}
              className={item.id === candidate?.id ? memoryPrimary : memoryButton}
              aria-pressed={item.id === candidate?.id}
              disabled={decision.isPending}
              onClick={() => {
                setSelected(item.id)
                setActiveCitation(null)
                decision.reset()
              }}
            >
              {item.title}
              <span className="ms-2">
                <MemoryStatus status={item.status} />
              </span>
            </button>
          ))}
        </div>
      )}
      {candidate && (
        <article className="border-hair bg-card rounded-2xl border p-5 sm:p-7">
          <h3 className="mb-4 text-xl font-semibold">{candidate.title}</h3>
          {!candidate.body_available ? (
            <p className="text-n600 text-sm leading-7">{t("staleHint")}</p>
          ) : (
            <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_224px]">
              <WikiMarkdown page={candidate} pages={pages} onCitation={setActiveCitation} />
              <WikiEvidence page={candidate} active={activeCitation} onSelect={setActiveCitation} />
            </div>
          )}
          {!!candidate.expected_target_revision && (
            <details className="border-hair mt-6 border-t pt-4">
              <summary className="cursor-pointer text-sm font-medium">
                {t("compareCurrent", { revision: candidate.expected_target_revision })}
              </summary>
              <div className="bg-rail mt-3 rounded-xl p-5">
                {previous.data?.body_available && !previous.error ? (
                  <WikiMarkdown page={previous.data} pages={pages} />
                ) : (
                  <p className="text-n600 text-sm">{t("previousUnavailable")}</p>
                )}
              </div>
            </details>
          )}
          {decision.error && (
            <p role="alert" className="text-danger mt-4 text-sm">
              {errorText(decision.error)}
            </p>
          )}
          {candidate.status === "pending" && (
            <div className="border-hair mt-6 flex flex-wrap items-center gap-3 border-t pt-5">
              <button
                className={memoryPrimary}
                disabled={!enabled || !candidate.body_available || decision.isPending}
                onClick={() => decision.mutate("approve")}
              >
                {t("approve")}
              </button>
              <button
                className={memoryButton}
                disabled={!enabled || decision.isPending}
                onClick={() => decision.mutate("reject")}
              >
                {t("reject")}
              </button>
              <p className="text-n500 text-xs">{t("approvalHint")}</p>
            </div>
          )}
        </article>
      )}
      {candidates.length >= 100 && <p className="text-n500 text-xs">{t("reviewLimit")}</p>}
    </section>
  )
}

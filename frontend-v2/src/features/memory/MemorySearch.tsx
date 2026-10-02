import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import type { MemoryBundle } from "@/shared/api/memory"
import { paths } from "@/shared/router/paths"
import { formatNumber } from "@/shared/lib/format"
import { DiagnosticData, MemoryStatus, memoryButton, memoryCard } from "@/shared/ui/MemoryDiagnostics"

const scoreKeys = ["score", "lexical_score", "dense_score", "rerank_score"] as const

export function MemorySearchResults({
  bundle,
  onSources,
}: {
  bundle: MemoryBundle
  onSources: (id: string) => void
}) {
  const { t } = useTranslation("memory")
  const score = (value: number | null | undefined) => (value == null ? t("unknown") : formatNumber(value))
  return (
    <section className={`${memoryCard} space-y-4`} aria-label={t("searchResults")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-medium">{t("searchResults")}</h2>
        <Link
          className={memoryButton}
          to={paths.memoryDebug(`request_id=${encodeURIComponent(bundle.request_id)}`)}
        >
          {t("inspectTrace")}
        </Link>
      </div>
      <p className="text-n500 text-xs break-all">
        {t("requestId")}: {bundle.request_id} · {t("indexGeneration")}:{" "}
        {bundle.index_generation ?? t("unknown")}
      </p>
      {bundle.degraded_reasons?.length > 0 && (
        <div className="bg-a100 text-a800 rounded-lg p-3 text-sm" role="status">
          {t("degraded")}: {bundle.degraded_reasons.join(", ")}
        </div>
      )}
      {bundle.items.length === 0 && <p className="text-n500 text-sm">{t("noSearchResults")}</p>}
      <ol className="space-y-3">
        {bundle.items.map((item, index) => (
          <li
            key={`${item.kind}-${item.id}-${item.revision}`}
            className="border-hair space-y-2 rounded-lg border p-3"
          >
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <span>
                {index + 1} · {item.kind} · {t("revision", { revision: item.revision })}
              </span>
              {item.confirmation_status && <MemoryStatus status={item.confirmation_status} />}
            </div>
            <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">{item.text}</p>
            <dl className="text-n500 flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {scoreKeys.map((key) => (
                <div key={key}>
                  <dt className="inline">{t(key)}</dt>
                  <dd className="ms-1 inline">{score(item[key])}</dd>
                </div>
              ))}
            </dl>
            {item.sources?.length > 0 && (
              <details>
                <summary className="text-n600 cursor-pointer text-xs">
                  {t("sourceReferences", { count: item.sources.length })}
                </summary>
                <DiagnosticData data={item.sources} label={t("sources")} />
              </details>
            )}
            {item.kind === "memory" && (
              <button className={memoryButton} onClick={() => onSources(item.id)}>
                {t("viewSources")}
              </button>
            )}
            {item.kind === "wiki" && (
              <Link className={memoryButton} to={paths.wikiPage(item.id)}>
                {t("wiki.readPage")}
              </Link>
            )}
          </li>
        ))}
      </ol>
      <details>
        <summary className="text-n600 cursor-pointer text-sm">{t("searchDiagnostics")}</summary>
        <DiagnosticData
          data={{
            scope: bundle.scope,
            budget: bundle.budget,
            lag: bundle.lag,
            route: bundle.route,
            time_context: bundle.time_context,
          }}
        />
      </details>
    </section>
  )
}

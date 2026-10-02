import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  type MemoryCleanup,
  type MemoryRecord,
  type MemoryRevision,
  type MemorySource,
} from "@/shared/api/memory"
import { formatDateTime } from "@/shared/lib/format"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { Spinner } from "@/shared/ui/Spinner"
import { DiagnosticData, MemoryStatus, memoryButton, memoryCard } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryCleanup, useMemoryHistory, useMemorySources } from "./api"

const detailTabs = ["sources", "history", "cleanup"] as const
type DetailTab = (typeof detailTabs)[number]

function memoryIsStopped(memory: MemoryRecord, cleanup?: MemoryCleanup) {
  return (
    memory.body_available === false ||
    memory.status === "DEPRECATED" ||
    cleanup?.stopped === true ||
    ["stopped_cleanup_pending", "cleaned"].includes(cleanup?.status ?? "")
  )
}

function SourceSnapshot({ source, bodyAvailable }: { source: MemorySource; bodyAvailable: boolean }) {
  const { t } = useTranslation("memory")
  const text =
    !bodyAvailable || source.body_available === false ? null : (source.body ?? source.content ?? source.text)
  return (
    <article className="border-hair space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs break-all">{source.source_kind ?? source.source_type ?? t("source")}</span>
        {source.status && <MemoryStatus status={source.status} />}
      </div>
      <dl className="text-n600 grid gap-1 text-xs">
        <div>
          <dt className="inline">{t("sourceId")}</dt>
          <dd className="ms-2 inline break-all">{source.id}</dd>
        </div>
        <div>
          <dt className="inline">{t("version")}</dt>
          <dd className="ms-2 inline">{String(source.source_revision ?? source.revision ?? t("unknown"))}</dd>
        </div>
        <div>
          <dt className="inline">{t("hash")}</dt>
          <dd className="ms-2 inline break-all">{source.content_hash ?? source.hash ?? t("unknown")}</dd>
        </div>
      </dl>
      {text != null ? (
        <p className="bg-hairsoft rounded-lg p-3 text-sm leading-relaxed break-words whitespace-pre-wrap">
          {text}
        </p>
      ) : (
        <p className="text-n500 text-sm">{t("cannotReconstruct")}</p>
      )}
      {(source.missing_reason || source.reason_code) && (
        <p className="text-n500 text-xs">{source.missing_reason ?? source.reason_code}</p>
      )}
    </article>
  )
}

function DetailSection({
  tab,
  ready,
  bodyAvailable,
  sources,
  history,
  cleanup,
  pending,
  onRefresh,
}: {
  tab: DetailTab
  ready: boolean
  bodyAvailable: boolean
  sources?: MemorySource[]
  history?: MemoryRevision[]
  cleanup?: MemoryCleanup
  pending: boolean
  onRefresh: () => void
}) {
  const { t } = useTranslation("memory")
  return (
    <>
      {ready && (
        <>
          {tab === "sources" && sources && (
            <div className="space-y-3">
              <p className="text-n500 text-xs">{t("immutableHint")}</p>
              {sources.length === 0 && <p className="text-n500 text-sm">{t("noSources")}</p>}
              {sources.map((source) => (
                <SourceSnapshot key={source.id} source={source} bodyAvailable={bodyAvailable} />
              ))}
            </div>
          )}
          {tab === "history" && history && (
            <ol className="space-y-3">
              {history.length === 0 && <li className="text-n500 text-sm">{t("noHistory")}</li>}
              {history.map((revision, index) => (
                <li
                  key={revision.id ?? `${revision.revision}-${index}`}
                  className="border-hair rounded-lg border p-3"
                >
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span>
                      {t("revision", { revision: revision.revision })} · {revision.reason ?? revision.action}
                    </span>
                    {revision.status && <MemoryStatus status={revision.status} />}
                  </div>
                  {bodyAvailable &&
                  revision.body_available !== false &&
                  (revision.summary ?? revision.value) != null ? (
                    <p className="text-sm whitespace-pre-wrap">
                      {revision.summary ?? String(revision.value)}
                    </p>
                  ) : (
                    <p className="text-n500 text-sm">{t("cannotReconstruct")}</p>
                  )}
                  {revision.created_at && (
                    <time className="text-n500 mt-2 block text-xs" dateTime={revision.created_at}>
                      {formatDateTime(revision.created_at)}
                    </time>
                  )}
                </li>
              ))}
            </ol>
          )}
          {tab === "cleanup" && cleanup && (
            <div className="space-y-3">
              <MemoryStatus status={cleanup.status} />
              <p className="text-n500 text-xs">{t("cleanupHint")}</p>
              <DiagnosticData data={cleanup} label={t("cleanup")} />
            </div>
          )}
        </>
      )}
      {tab === "cleanup" && (
        <button className={memoryButton} disabled={pending} onClick={onRefresh}>
          {t("refresh")}
        </button>
      )}
    </>
  )
}

export function MemoryDetail({ memory, onClose }: { memory: MemoryRecord; onClose: () => void }) {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const [tab, setTab] = useState<DetailTab>(memoryIsStopped(memory) ? "cleanup" : "sources")
  // Recheck current authority even when this detail holds an older selected record.
  const sources = useMemorySources(tab === "sources" ? memory.id : "")
  const history = useMemoryHistory(tab === "history" ? memory.id : "")
  const cleanup = useMemoryCleanup(memory.id)
  const query = tab === "sources" ? sources : tab === "history" ? history : cleanup
  const stopped = memoryIsStopped(memory, cleanup.data)
  const authorityReady = cleanup.isSuccess && !cleanup.isFetching
  const bodyAvailable = authorityReady && cleanup.data?.status === "active" && !stopped
  const sectionReady = authorityReady && !query.isFetching && !query.error
  const error = query.error ?? cleanup.error
  return (
    <section className={`${memoryCard} space-y-4`} aria-label={t("detail")}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-medium">{t("detail")}</h2>
          <p className="text-n500 mt-1 text-xs break-all">
            {memory.id}
            {bodyAvailable && <> · {t("revision", { revision: memory.revision })}</>}
          </p>
        </div>
        <button className={memoryButton} onClick={onClose}>
          {t("close")}
        </button>
      </div>
      {stopped ? (
        <p className="text-n500 text-sm">{t("cannotReconstruct")}</p>
      ) : bodyAvailable ? (
        <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">{memory.summary}</p>
      ) : null}
      <div className="flex flex-wrap gap-2" role="tablist" aria-label={t("detail")}>
        {detailTabs.map((value) => (
          <button
            key={value}
            role="tab"
            aria-selected={tab === value}
            className={memoryButton}
            onClick={() => setTab(value)}
          >
            {t(value)}
          </button>
        ))}
      </div>
      {(query.isFetching || cleanup.isFetching) && <Spinner />}
      {error && (
        <p role="alert" className="text-danger text-sm">
          {errorText(error)}
        </p>
      )}
      <DetailSection
        tab={tab}
        ready={sectionReady}
        bodyAvailable={bodyAvailable}
        sources={sources.data?.sources}
        history={history.data?.revisions}
        cleanup={cleanup.data}
        pending={cleanup.isFetching}
        onRefresh={() => void cleanup.refetch()}
      />
    </section>
  )
}

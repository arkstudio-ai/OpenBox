import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import {
  BookOpen,
  ChevronRight,
  CircleCheck,
  MessageSquare,
  Pencil,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react"
import type { MemoryCleanup, MemoryRecord, MemoryRevision, MemorySource } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { formatDateTime, formatSince } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { useEffect } from "react"
import { useMemoryCleanup, useMemoryDetail, useMemoryHistory, useMemorySources } from "../api"
import { useBackState } from "./back"
import { memoryIsStopped, type MemoryTopic } from "./data"
import { Sheet } from "./Sheet"
import { button, dangerButton, iconButton } from "./ui"

/** The label key used when a source kind or change reason has none of its own. */
const OTHER = "other"

export interface MemorySheetProps {
  memory: MemoryRecord
  scopeName: string
  projectId: string
  topics: MemoryTopic[]
  busy: boolean
  onEdit: () => void
  onForget: () => void
  onClose: () => void
  /** The memory changed since the list was read; continue with the current one. */
  onCurrent?: (memory: MemoryRecord) => void
}

/** One memory in full: what it says, where it came from and how it changed.
 *  Its text comes from a fresh, re-authorized read on every open, never from
 *  the list row; the cleanup status only reports how forgetting is going. */
export function MemorySheet({
  memory,
  scopeName,
  projectId,
  topics,
  busy,
  onEdit,
  onForget,
  onClose,
  onCurrent,
}: MemorySheetProps) {
  const { t } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const current = useMemoryDetail(memory.id)
  const cleanup = useMemoryCleanup(memory.id)
  const sources = useMemorySources(memory.id)
  const history = useMemoryHistory(memory.id)
  const stopped = memoryIsStopped(memory, cleanup.data)
  // Forgetting stays possible when the text is no longer readable.
  const forgettable = cleanup.isSuccess && !cleanup.isFetching
  const fresh = current.isSuccess && !current.isFetching ? current.data : undefined
  const bodyAvailable = fresh?.body_available === true && fresh.status === "ACTIVE" && !stopped
  useEffect(() => {
    if (fresh?.body_available && fresh.revision !== memory.revision) onCurrent?.(fresh)
  }, [fresh, memory.revision, onCurrent])
  const error = current.error ?? cleanup.error ?? sources.error ?? history.error
  return (
    <Sheet label={t("detail.title")} onClose={onClose}>
      <header className="border-hair flex items-center justify-between gap-3 border-b px-5 py-3.5">
        <h2 className="text-lg font-medium">{t("detail.title")}</h2>
        <button type="button" className={iconButton} aria-label={t("detail.close")} onClick={onClose}>
          <X size={17} />
        </button>
      </header>
      <section
        aria-label={t("detail.title")}
        className="scr flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-5 py-5"
      >
        {error && (
          <p role="alert" className="bg-dangersoft text-dangerink rounded-xl px-3 py-2 text-sm">
            {errorText(error)}
          </p>
        )}
        <Statement
          stopped={stopped}
          text={bodyAvailable ? fresh.summary : null}
          loading={current.isPending || current.isFetching || cleanup.isPending}
          cleanup={cleanup.data}
          checking={cleanup.isFetching}
          onCheck={() => void cleanup.refetch()}
        />
        <Facts memory={fresh ?? memory} scopeName={scopeName} />
        {bodyAvailable && topics.some((topic) => topic.page_id) && (
          <Topics topics={topics} projectId={projectId} />
        )}
        {bodyAvailable && (
          <Sources sources={sources.data?.sources} loading={sources.isPending || sources.isFetching} />
        )}
        {bodyAvailable && <History revisions={history.data?.revisions} loading={history.isPending} />}
      </section>
      {!stopped && (
        <footer className="border-hair flex justify-end gap-2 border-t px-5 py-3.5">
          <button type="button" className={button} disabled={busy || !bodyAvailable} onClick={onEdit}>
            <Pencil size={14} aria-hidden />
            {t("memory.edit")}
          </button>
          <button type="button" className={dangerButton} disabled={busy || !forgettable} onClick={onForget}>
            <Trash2 size={14} aria-hidden />
            {t("memory.forget")}
          </button>
        </footer>
      )}
    </Sheet>
  )
}

function Forgotten({
  cleanup,
  checking,
  onCheck,
}: {
  cleanup?: MemoryCleanup
  checking: boolean
  onCheck: () => void
}) {
  const { t } = useTranslation("knowledge")
  const done = cleanup?.status === "cleaned"
  return (
    <div className="bg-hairsoft rounded-2xl px-4 py-4" role="status">
      <p className="text-ink flex items-center gap-2 font-medium">
        {done ? (
          <CircleCheck size={16} className="text-sage" aria-hidden />
        ) : (
          <Spinner className="size-3.5" />
        )}
        {t("detail.forgotten")}
      </p>
      <p className="text-n700 mt-1.5 text-sm leading-relaxed">
        {t(done ? "detail.cleanupDone" : "detail.cleanupPending")}
      </p>
      {!done && (
        <button type="button" className={button + " mt-3"} disabled={checking} onClick={onCheck}>
          <RefreshCw size={13} aria-hidden className={checking ? "animate-spin" : ""} />
          {t("detail.checkAgain")}
        </button>
      )}
    </div>
  )
}

function Topics({ topics, projectId }: { topics: MemoryTopic[]; projectId: string }) {
  const { t } = useTranslation("knowledge")
  const back = useBackState()
  return (
    <div>
      <h3 className="text-n600 mb-2 text-xs font-medium">{t("detail.topics")}</h3>
      <div className="flex flex-wrap gap-2">
        {topics
          .filter((topic) => topic.page_id)
          .map((topic) => (
            <Link
              key={topic.id}
              to={paths.wikiPage(topic.page_id!, projectId)}
              state={back}
              className="border-hair text-n800 hover:border-n400 hover:text-a700 inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm transition-colors"
            >
              <BookOpen size={13} aria-hidden className="text-a700" />
              {topic.title}
              <ChevronRight size={13} aria-hidden className="text-n500" />
            </Link>
          ))}
      </div>
    </div>
  )
}

function Sources({ sources, loading }: { sources?: MemorySource[]; loading: boolean }) {
  const { t } = useTranslation("knowledge")
  return (
    <div>
      <h3 className="text-ink text-sm font-medium">{t("detail.sources")}</h3>
      <p className="text-n600 mt-0.5 mb-3 text-xs">{t("detail.sourcesHint")}</p>
      {loading ? (
        <Spinner />
      ) : !sources?.length ? (
        <p className="text-n600 text-sm">{t("detail.noSources")}</p>
      ) : (
        <ul className="space-y-2.5">
          {/* What backs the memory now first; replaced wording after it. */}
          {[
            ...sources.filter((source) => !source.superseded),
            ...sources.filter((source) => source.superseded),
          ].map((source) => (
            <SourceItem key={source.id} source={source} />
          ))}
        </ul>
      )}
    </div>
  )
}

/** What the memory says now, or why it cannot be shown. */
function Statement({
  stopped,
  text,
  loading,
  cleanup,
  checking,
  onCheck,
}: {
  stopped: boolean
  text: string | null
  loading: boolean
  cleanup: MemoryCleanup | undefined
  checking: boolean
  onCheck: () => void
}) {
  const { t } = useTranslation("knowledge")
  if (stopped) return <Forgotten cleanup={cleanup} checking={checking} onCheck={onCheck} />
  if (text !== null)
    return <p className="text-ink text-xl leading-relaxed break-words whitespace-pre-wrap">{text}</p>
  if (loading) return <Spinner />
  return <p className="text-n600 text-sm">{t("detail.unavailable")}</p>
}

/** Where a memory lives and when it was saved and last changed. */
function Facts({ memory, scopeName }: { memory: MemoryRecord; scopeName: string }) {
  const { t } = useTranslation("knowledge")
  const rows = [
    ["detail.scope", scopeName],
    ["detail.created", memory.created_at ? formatDateTime(memory.created_at) : ""],
    ["detail.updated", memory.updated_at ? formatDateTime(memory.updated_at) : ""],
  ].filter(([, value]) => value)
  return (
    <dl className="text-n600 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt>{t(label)}</dt>
          <dd className="text-n800">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function SourceItem({ source }: { source: MemorySource }) {
  const { t } = useTranslation("knowledge")
  const kind = source.source_kind ?? source.source_type ?? OTHER
  const body = source.body_available === false ? null : (source.body ?? source.content ?? source.text)
  const session = typeof source.session_id === "string" ? source.session_id : null
  return (
    <li className="bg-hairsoft/70 rounded-xl px-3.5 py-3">
      <div className="text-n600 flex items-center justify-between gap-2 text-xs">
        <span>{t(`detail.sourceKind.${kind}`, { defaultValue: t("detail.sourceKind.other") })}</span>
        {source.created_at && <time dateTime={source.created_at}>{formatSince(source.created_at)}</time>}
      </div>
      {body != null ? (
        <p className="text-ink mt-1.5 text-sm leading-relaxed break-words whitespace-pre-wrap">{body}</p>
      ) : (
        <p className="text-n600 mt-1.5 text-sm">
          {t(source.superseded ? "detail.supersededSource" : "detail.sourceUnavailable")}
        </p>
      )}
      {source.changes?.map((change, index) => (
        <div key={index} className="border-hair mt-2.5 border-t pt-2.5">
          <p className="text-n600 text-xs">{t("detail.yourCorrection")}</p>
          <p className="text-ink mt-1 text-sm leading-relaxed break-words whitespace-pre-wrap">
            {change.body}
          </p>
          {change.session_id && (
            <Link
              to={paths.chat(change.session_id)}
              className="text-a700 mt-2 inline-flex items-center gap-1 text-xs hover:underline"
            >
              <MessageSquare size={12} aria-hidden />
              {t("detail.openChat")}
            </Link>
          )}
        </div>
      ))}
      {session && (
        <Link
          to={paths.chat(session)}
          className="text-a700 mt-2 inline-flex items-center gap-1 text-xs hover:underline"
        >
          <MessageSquare size={12} aria-hidden />
          {t("detail.openChat")}
        </Link>
      )}
    </li>
  )
}

function History({ revisions, loading }: { revisions?: MemoryRevision[]; loading: boolean }) {
  const { t } = useTranslation("knowledge")
  return (
    <details className="group">
      <summary className="text-ink flex cursor-pointer list-none items-center gap-1.5 text-sm font-medium">
        <ChevronRight size={14} aria-hidden className="text-n500 transition-transform group-open:rotate-90" />
        {t("detail.history")}
        {revisions && <span className="text-n500 font-normal">{revisions.length}</span>}
      </summary>
      {loading ? (
        <Spinner className="mt-3" />
      ) : !revisions?.length ? (
        <p className="text-n600 mt-3 text-sm">{t("detail.noHistory")}</p>
      ) : (
        <ol className="border-hair ms-1.5 mt-3 space-y-4 border-s ps-4">
          {revisions.map((revision, index) => {
            const text = revision.body_available === false ? null : (revision.summary ?? null)
            const reason = revision.reason ?? revision.action ?? OTHER
            return (
              <li key={revision.id ?? `${revision.revision}-${index}`} className="relative">
                <span className="bg-n400 absolute -start-[1.3rem] top-1.5 size-2 rounded-full" aria-hidden />
                <p className="text-n600 text-xs">
                  {t(`detail.reason.${reason}`, { defaultValue: t("detail.reason.other") })}
                  {revision.created_at && (
                    <>
                      {" · "}
                      <time dateTime={revision.created_at}>{formatDateTime(revision.created_at)}</time>
                    </>
                  )}
                </p>
                {text ? (
                  <p className="text-n800 mt-1 text-sm leading-relaxed break-words whitespace-pre-wrap">
                    {text}
                  </p>
                ) : (
                  <p className="text-n500 mt-1 text-sm">{t("detail.unavailable")}</p>
                )}
              </li>
            )
          })}
        </ol>
      )}
    </details>
  )
}

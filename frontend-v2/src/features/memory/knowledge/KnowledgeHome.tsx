import { useEffect, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useSearchParams } from "react-router"
import { Plus, Upload } from "lucide-react"
import type { MemoryRecord } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useMemoryScope } from "../api"
import { readView, useKnowledge, type Knowledge, type KnowledgeView } from "./data"
import { FileDropzone, FileList } from "./FileList"
import { ForgetDialog } from "./ForgetDialog"
import { MemoryEditor } from "./MemoryEditor"
import { MemoryList } from "./MemoryList"
import { MemorySheet } from "./MemorySheet"
import { Intro, NoResults, Quiet, SearchBar, Section, Skeleton, ViewTabs, Welcome } from "./parts"
import { TopicGrid } from "./TopicGrid"
import { button, primaryButton, textButton } from "./ui"
import { useFileUpload } from "./useFileUpload"
import { useMemoryActions } from "./useMemoryActions"

const PREVIEW = { memories: 5, topics: 6, files: 4 } as const

function useDebounced(value: string, delay: number) {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return settled
}

/** The URL holds what is being looked at — section, scope and search — so a
 *  reload, a shared link or the back button returns to the same place. */
function useKnowledgeLocation() {
  const [params, setParams] = useSearchParams()
  const update = (changes: Record<string, string>) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const [name, value] of Object.entries(changes)) {
          if (value) next.set(name, value)
          else next.delete(name)
        }
        return next
      },
      { replace: true },
    )
  return {
    view: readView(params.get("view")),
    projectId: params.get("project") ?? "",
    initialQuery: params.get("q") ?? "",
    update,
  }
}

/** The 知识库 home: what the assistant remembers, the topics it organizes and
 *  the files it reads — one search, one scope, one place to change any of it. */
export function KnowledgeHome() {
  const { t } = useTranslation("knowledge")
  const location = useKnowledgeLocation()
  const { view, projectId, update } = location
  const [search, setSearch] = useState(location.initialQuery)
  const query = useDebounced(search.trim(), 250)
  const data = useKnowledge(projectId, query)
  const actions = useMemoryActions()
  const setView = (next: KnowledgeView) => update({ view: next === "overview" ? "" : next })
  const upload = useFileUpload(projectId, () => setView("files"))
  const canUpload = Boolean(data.capability.data?.enabled)
  useEffect(() => {
    if (query !== location.initialQuery) update({ q: query })
    // Only a settled search is written back; the URL never drives the input.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])
  const projects = data.projects.data ?? []
  const scopeName = (memory: MemoryRecord) =>
    memory.project_id ? (data.projectName(memory.project_id) ?? "") : t("personal")
  const { dialog, detail } = actions
  return (
    <div className="mx-auto w-full max-w-5xl px-4 pt-6 pb-16 sm:px-8 sm:pt-9">
      {upload.picker}
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
        <div className="max-w-2xl min-w-0">
          <h1 className="text-ink text-3xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-n600 mt-2 text-md leading-relaxed">{t("subtitle")}</p>
        </div>
        <div className="flex flex-none gap-2">
          <button type="button" className={button} disabled={!canUpload || upload.pending} onClick={upload.choose}>
            <Upload size={15} aria-hidden />
            {t(upload.pending ? "uploading" : "uploadFile")}
          </button>
          <button type="button" className={primaryButton} onClick={() => actions.open({ kind: "create" })}>
            <Plus size={15} aria-hidden />
            {t("addMemory")}
          </button>
        </div>
      </header>
      <div className="mt-7 space-y-4">
        <SearchBar
          value={search}
          onChange={setSearch}
          projects={projects}
          projectId={projectId}
          onProject={(id) => {
            actions.setDetail(null)
            update({ project: id })
          }}
        />
        <ViewTabs view={view} counts={counts(data)} onChange={setView} />
      </div>
      <LoadError data={data} />
      <div className="mt-6">
        <ViewBody
          view={view}
          data={data}
          query={query}
          projectId={projectId}
          canUpload={canUpload}
          busy={actions.write.isPending}
          upload={upload}
          scopeName={scopeName}
          setView={setView}
          actions={actions}
        />
      </div>
      {detail && (
        <MemorySheet
          key={`${detail.id}:${detail.revision}:${detail.status}`}
          memory={detail}
          scopeName={scopeName(detail)}
          projectId={projectId}
          topics={data.topicsOf.get(detail.id) ?? []}
          busy={actions.write.isPending}
          onEdit={() => actions.open({ kind: "edit", memory: detail })}
          onForget={() => actions.open({ kind: "forget", memory: detail })}
          // A dialog opened from the sheet owns Escape until it closes.
          onClose={() => !dialog && actions.setDetail(null)}
        />
      )}
      {dialog?.kind === "forget" ? (
        <ForgetDialog
          key={dialog.memory.id}
          memory={dialog.memory}
          pending={actions.write.isPending}
          error={actions.error}
          onClose={actions.close}
          onConfirm={(sourceIds) => actions.write.mutate({ kind: "forget", memory: dialog.memory, sourceIds })}
        />
      ) : dialog ? (
        <MemoryEditor
          key={dialog.kind === "edit" ? dialog.memory.id : "new"}
          memory={dialog.kind === "edit" ? dialog.memory : undefined}
          projects={projects}
          projectId={projectId}
          pending={actions.write.isPending}
          error={actions.error}
          onClose={actions.close}
          onSubmit={(draft) =>
            actions.write.mutate(
              dialog.kind === "edit"
                ? { kind: "edit", memory: dialog.memory, summary: draft.summary }
                : { kind: "create", summary: draft.summary, projectId: draft.projectId },
            )
          }
        />
      ) : null}
    </div>
  )
}

function counts(data: Knowledge): Partial<Record<KnowledgeView, string>> {
  const more = (count: number, hasMore: boolean) => String(count) + (hasMore ? "+" : "")
  return {
    memories: data.memories.data ? more(data.memoryList.length, data.memoryLimited) : undefined,
    topics: data.library.data ? more(data.topics.length, data.library.hasNextPage) : undefined,
    files: data.documents.data ? more(data.files.length, data.documents.hasNextPage) : undefined,
  }
}

function LoadError({ data }: { data: Knowledge }) {
  const { t } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const qc = useQueryClient()
  const { key } = useMemoryScope()
  if (!data.error) return null
  return (
    <div
      role="alert"
      className="bg-dangersoft text-dangerink mt-5 flex flex-wrap items-center justify-between gap-2 rounded-2xl px-4 py-3 text-sm"
    >
      <span>
        {t("loadFailed")} {errorText(data.error)}
      </span>
      <button type="button" className={textButton} onClick={() => void qc.invalidateQueries({ queryKey: key })}>
        {t("retry")}
      </button>
    </div>
  )
}

interface ViewProps {
  view: KnowledgeView
  data: Knowledge
  query: string
  projectId: string
  canUpload: boolean
  busy: boolean
  upload: ReturnType<typeof useFileUpload>
  scopeName: (memory: MemoryRecord) => string
  setView: (view: KnowledgeView) => void
  actions: ReturnType<typeof useMemoryActions>
}

function ViewBody(props: ViewProps) {
  const { view, data, query, projectId, canUpload, upload, actions } = props
  const loading = data.memories.isPending || data.library.isPending || data.documents.isPending
  const empty = !data.memoryList.length && !data.topics.length && !data.files.length
  if (view === "overview" && loading) return <Skeleton rows={4} />
  if (view === "overview" && empty && !query && !data.error)
    return (
      <Welcome
        projectId={projectId}
        canUpload={canUpload}
        onAdd={() => actions.open({ kind: "create" })}
        onUpload={upload.choose}
      />
    )
  if (view === "overview" && empty && query && !loading) return <NoResults query={query} projectId={projectId} />
  if (view === "memories") return <MemoriesView {...props} />
  if (view === "topics") return <TopicsView {...props} />
  if (view === "files") return <FilesView {...props} />
  return <Overview {...props} />
}

function Overview(props: ViewProps) {
  const { t } = useTranslation("knowledge")
  const { data, query, projectId, canUpload, upload, setView, actions } = props
  const memoryCount = counts(data)
  return (
    <div className="space-y-10">
      {(!query || data.memoryList.length > 0) && (
        <Section
          title={t("section.memories")}
          count={memoryCount.memories}
          onSeeAll={data.memoryList.length > PREVIEW.memories ? () => setView("memories") : undefined}
        >
          {data.memoryList.length ? (
            <Memories {...props} memories={data.memoryList.slice(0, PREVIEW.memories)} />
          ) : (
            <Quiet
              action={
                <button type="button" className={textButton} onClick={() => actions.open({ kind: "create" })}>
                  <Plus size={14} aria-hidden />
                  {t("addMemory")}
                </button>
              }
            >
              {t("empty.memories")}
            </Quiet>
          )}
        </Section>
      )}
      {(!query || data.topics.length > 0) && (
        <Section
          title={t("section.topics")}
          count={memoryCount.topics}
          onSeeAll={data.topics.length > PREVIEW.topics || data.library.hasNextPage ? () => setView("topics") : undefined}
        >
          {data.topics.length ? (
            <TopicGrid rail topics={data.topics.slice(0, PREVIEW.topics)} projectId={projectId} query={query} />
          ) : (
            <Quiet>{t("empty.topics")}</Quiet>
          )}
        </Section>
      )}
      {(!query || data.files.length > 0) && (
        <Section
          title={t("section.files")}
          count={memoryCount.files}
          onSeeAll={data.files.length > PREVIEW.files || data.documents.hasNextPage ? () => setView("files") : undefined}
        >
          {data.files.length ? (
            <FileList files={data.files.slice(0, PREVIEW.files)} projectId={projectId} query={query} />
          ) : (
            <FileDropzone compact disabled={!canUpload} onChoose={upload.choose} onDrop={(files) => void upload.submit(files)} />
          )}
        </Section>
      )}
    </div>
  )
}

function Memories(props: ViewProps & { memories: MemoryRecord[] }) {
  const { data, query, projectId, busy, scopeName, actions, memories } = props
  return (
    <MemoryList
      memories={memories}
      query={query}
      projectId={projectId}
      busy={busy}
      topicsOf={data.topicsOf}
      scopeName={scopeName}
      onOpen={actions.setDetail}
      onEdit={(memory) => actions.open({ kind: "edit", memory })}
      onForget={(memory) => actions.open({ kind: "forget", memory })}
    />
  )
}

function MemoriesView(props: ViewProps) {
  const { t } = useTranslation("knowledge")
  const { data, query, projectId, actions } = props
  if (data.memories.isPending) return <Skeleton />
  return (
    <>
      <Intro>{t("section.memoriesHint")}</Intro>
      {data.memoryList.length ? (
        <Memories {...props} memories={data.memoryList} />
      ) : query ? (
        <NoResults query={query} projectId={projectId} />
      ) : (
        <Quiet
          action={
            <button type="button" className={primaryButton} onClick={() => actions.open({ kind: "create" })}>
              <Plus size={14} aria-hidden />
              {t("addMemory")}
            </button>
          }
        >
          {t("empty.memories")}
        </Quiet>
      )}
      {data.memoryLimited && (
        <p className="text-n500 mt-3 text-xs">{t("memory.listLimit", { count: data.memoryList.length })}</p>
      )}
    </>
  )
}

function TopicsView({ data, query, projectId }: ViewProps) {
  const { t } = useTranslation("knowledge")
  if (data.library.isPending) return <Skeleton />
  return (
    <>
      <Intro>{t("section.topicsHint")}</Intro>
      {data.topics.length ? (
        <TopicGrid topics={data.topics} projectId={projectId} query={query} />
      ) : query ? (
        <NoResults query={query} projectId={projectId} />
      ) : (
        <Quiet>{t("empty.topics")}</Quiet>
      )}
      <LoadMore
        visible={data.library.hasNextPage}
        pending={data.library.isFetchingNextPage}
        onClick={() => void data.library.fetchNextPage()}
      />
    </>
  )
}

function FilesView({ data, query, projectId, canUpload, upload }: ViewProps) {
  const { t } = useTranslation("knowledge")
  return (
    <>
      <Intro>{t("section.filesHint")}</Intro>
      <FileDropzone disabled={!canUpload || upload.pending} onChoose={upload.choose} onDrop={(files) => void upload.submit(files)} />
      {upload.error && (
        <p role="alert" className="bg-dangersoft text-dangerink mt-3 rounded-xl px-3.5 py-2.5 text-sm">
          {upload.error}
        </p>
      )}
      <div className="mt-4">
        {data.documents.isPending ? (
          <Skeleton rows={2} />
        ) : data.files.length ? (
          <FileList files={data.files} projectId={projectId} query={query} />
        ) : query ? (
          <NoResults query={query} projectId={projectId} />
        ) : null}
      </div>
      <LoadMore
        visible={data.documents.hasNextPage}
        pending={data.documents.isFetchingNextPage}
        onClick={() => void data.documents.fetchNextPage()}
      />
    </>
  )
}

function LoadMore({ visible, pending, onClick }: { visible: boolean; pending: boolean; onClick: () => void }) {
  const { t } = useTranslation("knowledge")
  if (!visible) return null
  return (
    <div className="mt-5 flex justify-center">
      <button type="button" className={button} disabled={pending} onClick={onClick}>
        {t(pending ? "loading" : "loadMore")}
      </button>
    </div>
  )
}

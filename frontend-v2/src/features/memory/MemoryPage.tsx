import { useRef, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Brain, Plus, Search } from "lucide-react"
import { ApiError } from "@/shared/api/http"
import { memoryApi, type MemoryRecord, type MemoryTab } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { formatDateTime } from "@/shared/lib/format"
import { Spinner } from "@/shared/ui/Spinner"
import {
  MemoryStatus,
  memoryButton,
  memoryCard,
  memoryInput,
  memoryPrimary,
} from "@/shared/ui/MemoryDiagnostics"
import { useMemories, useMemoryProjects, useMemoryScope } from "./api"
import { MemoryDetail } from "./MemoryDetail"
import { MemoryEditor, type MemoryEdit, type MemoryEditInput } from "./MemoryEditor"
import { MemorySearchResults } from "./MemorySearch"
import { WikiPanel } from "./WikiPanel"
import { MemoryGroups } from "./MemoryGroups"

const memoryTabs = ["active", "forgotten"] as const

type WriteAction = MemoryEdit & MemoryEditInput

function MemoryWorkspace() {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const [tab, setTab] = useState<MemoryTab>("active")
  const [projectId, setProjectId] = useState("")
  const [query, setQuery] = useState("")
  const [selected, setSelected] = useState<MemoryRecord | null>(null)
  const [edit, setEdit] = useState<MemoryEdit | null>(null)
  const requestId = useRef<string | null>(null)
  const memories = useMemories(tab, projectId)
  const projects = useMemoryProjects()
  const search = useMutation({ mutationFn: () => memoryApi.search(query.trim(), projectId) })
  const write = useMutation({
    mutationFn: async (input: WriteAction) => {
      requestId.current ??= crypto.randomUUID()
      const id = requestId.current
      if (input.action === "create") return memoryApi.create(input.summary, projectId, id)
      if (!input.memory) throw new Error("Missing memory revision")
      if (input.action === "correct") return memoryApi.correct(input.memory, input.summary, id)
      if (input.action === "forget") return memoryApi.forget(input.memory, id, input.sourceIds)
      throw new Error("Unknown memory action")
    },
    onSuccess: (_data, input) => {
      requestId.current = null
      setEdit(null)
      search.reset()
      if (input.action === "forget" && input.memory) {
        // The forget response confirms authority loss, but carries no new revision.
        setSelected({
          ...input.memory,
          summary: "",
          value: undefined,
          status: "DEPRECATED",
          body_available: false,
        })
        // Reset inactive snapshots too; invalidation alone retains their old bodies.
        void qc.resetQueries({ queryKey: key })
      } else {
        setSelected(null)
        void qc.invalidateQueries({ queryKey: key })
      }
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status < 500) requestId.current = null
      if (error instanceof ApiError && error.status === 409) void qc.invalidateQueries({ queryKey: key })
    },
  })
  const openEditor = (next: MemoryEdit) => {
    requestId.current = null
    write.reset()
    setEdit(next)
  }
  const chooseProject = (id: string) => {
    setProjectId(id)
    setSelected(null)
    search.reset()
  }
  const writeError =
    write.error instanceof ApiError && write.error.status === 409
      ? t("revisionConflict")
      : write.error
        ? errorText(write.error)
        : null
  return (
    <div className="scr min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto flex max-w-6xl flex-col gap-5">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-medium">
              <Brain size={24} aria-hidden />
              {t("title")}
            </h1>
            <p className="text-n600 mt-2 max-w-2xl text-sm leading-relaxed">{t("subtitle")}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              className={`${memoryPrimary} flex items-center gap-1.5`}
              disabled={write.isPending}
              onClick={() => openEditor({ action: "create" })}
            >
              <Plus size={15} aria-hidden />
              {t("create")}
            </button>
          </div>
        </header>
        <section className={`${memoryCard} space-y-3`} aria-label={t("filters")}>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-w-44 flex-1 flex-col gap-1.5 text-xs">
              <span>{t("project")}</span>
              <select
                className={memoryInput}
                value={projectId}
                onChange={(event) => chooseProject(event.target.value)}
              >
                <option value="">{t("allProjects")}</option>
                {projects.data?.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              className={memoryButton}
              disabled={memories.isFetching}
              onClick={() => void memories.refetch()}
            >
              {t("refresh")}
            </button>
          </div>
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              if (query.trim()) search.mutate()
            }}
          >
            <label className="flex min-w-0 flex-1 flex-col gap-1.5 text-xs">
              <span>{t("searchLabel")}</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                maxLength={2000}
                placeholder={t("searchPlaceholder")}
                className={memoryInput}
              />
            </label>
            <button
              className={`${memoryButton} mt-auto flex items-center gap-1.5`}
              disabled={search.isPending || !query.trim()}
              type="submit"
            >
              <Search size={15} aria-hidden />
              {t(search.isPending ? "searching" : "search")}
            </button>
          </form>
          <p className="text-n600 text-xs">{t("searchScopeHint")}</p>
        </section>
        {search.error && (
          <p className="text-danger text-sm" role="alert">
            {errorText(search.error)}
          </p>
        )}
        {search.data && (
          <MemorySearchResults
            bundle={search.data}
            onSources={(id) => {
              const memory = memories.data?.memories.find((row) => row.id === id)
              if (memory) setSelected(memory)
              else {
                const hit = search.data.items.find((row) => row.id === id)
                if (hit)
                  setSelected({
                    id,
                    summary: hit.text,
                    revision: hit.revision,
                    type: hit.kind,
                    scope: "LONG_TERM",
                    status: "ACTIVE",
                  })
              }
            }}
          />
        )}
        <div className="flex flex-wrap gap-2" role="tablist" aria-label={t("statusTabs")}>
          {memoryTabs.map((value) => (
            <button
              key={value}
              role="tab"
              aria-selected={tab === value}
              className={tab === value ? memoryPrimary : memoryButton}
              onClick={() => {
                setTab(value)
                setSelected(null)
              }}
            >
              {t(`tabs.${value}`)}
            </button>
          ))}
        </div>
        {writeError && !edit && (
          <p className="text-danger text-sm" role="alert">
            {writeError}
          </p>
        )}
        {memories.error && (
          <p className="text-danger text-sm" role="alert">
            {errorText(memories.error)}
          </p>
        )}
        {memories.isLoading && <Spinner />}
        <div className={`grid items-start gap-4 ${selected ? "lg:grid-cols-2" : ""}`}>
          <div className="space-y-3">
            {memories.data?.memories.length === 0 && (
              <div className={`${memoryCard} text-n500 py-10 text-center text-sm`}>{t(`empty.${tab}`)}</div>
            )}
            <MemoryGroups
              memories={memories.data?.memories ?? []}
              projectId={projectId}
              active={tab === "active"}
            >
              {(memory) => (
                <article key={memory.id} className={`${memoryCard} space-y-3`}>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap gap-1.5">
                      <MemoryStatus status={memory.status} />
                    </div>
                    <span className="text-n500 text-xs">{t("revision", { revision: memory.revision })}</span>
                  </div>
                  <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">
                    {memory.body_available === false || memory.status === "DEPRECATED"
                      ? t("cannotReconstruct")
                      : memory.summary}
                  </p>
                  <p className="text-n500 text-xs">
                    {memory.project_id
                      ? (projects.data?.find((project) => project.id === memory.project_id)?.name ??
                        memory.project_id)
                      : t("personal")}
                    {memory.updated_at && (
                      <>
                        {" "}
                        · <time dateTime={memory.updated_at}>{formatDateTime(memory.updated_at)}</time>
                      </>
                    )}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <button className={memoryButton} onClick={() => setSelected(memory)}>
                      {t("viewSources")}
                    </button>
                    {memory.status === "ACTIVE" && (
                      <button
                        className={memoryButton}
                        disabled={write.isPending}
                        onClick={() => openEditor({ action: "correct", memory })}
                      >
                        {t("correct")}
                      </button>
                    )}
                    {!["DEPRECATED", "EXPIRED"].includes(memory.status) && (
                      <button
                        className={`${memoryButton} text-dangerink`}
                        disabled={write.isPending}
                        onClick={() => openEditor({ action: "forget", memory })}
                      >
                        {t("forget")}
                      </button>
                    )}
                  </div>
                </article>
              )}
            </MemoryGroups>
            {memories.data && (
              <p className="text-n500 text-xs">
                {t("listLimit", { count: memories.data.memories.length, limit: 100 })}
              </p>
            )}
          </div>
          {selected && (
            <MemoryDetail
              key={`${selected.id}:${selected.revision}:${selected.status}`}
              memory={selected}
              onClose={() => setSelected(null)}
            />
          )}
        </div>
        <WikiPanel key={projectId} projectId={projectId} />
        {edit && (
          <MemoryEditor
            key={`${edit.action}:${edit.memory?.id ?? "new"}`}
            edit={edit}
            pending={write.isPending}
            error={writeError}
            onClose={() => setEdit(null)}
            onSubmit={(input) => write.mutate({ ...edit, ...input })}
          />
        )}
      </div>
    </div>
  )
}

export function MemoryPage() {
  const { userId, workspaceId } = useMemoryScope()
  // Unmount bounded snapshots, drafts and replayable mutation state on scope changes.
  return <MemoryWorkspace key={`${userId}:${workspaceId}`} />
}

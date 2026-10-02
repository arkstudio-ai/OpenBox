import { useRef, useState } from "react"
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { wikiApi, type WikiJob, type WikiPage } from "../wiki-api"

interface CompileProps {
  projectId: string
  initial?: WikiPage
  scopeName?: string
  onQueued: (job: WikiJob) => void
  onClose: () => void
}

function useCompileForm({ projectId, initial, onQueued }: CompileProps) {
  const { t } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const [title, setTitle] = useState(initial?.title ?? "")
  const [slug, setSlug] = useState(() => initial?.slug ?? "page-" + crypto.randomUUID().slice(0, 12))
  const [selected, setSelected] = useState<string[]>(
    initial?.memory_dependencies?.map((item) => item.id) ?? [],
  )
  const [consent, setConsent] = useState(false)
  const requestId = useRef<string | null>(null)
  const capability = useQuery({ queryKey: [...key, "wiki-capabilities"], queryFn: wikiApi.capabilities })
  const sources = useInfiniteQuery({
    queryKey: [...key, "wiki-compile-sources", projectId],
    queryFn: ({ pageParam }) => wikiApi.compileSources(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  })
  const memories = sources.data?.pages.flatMap((batch) => batch.memories) ?? []
  const uniqueSources = new Map(
    memories
      .filter((memory) => selected.includes(memory.id))
      .flatMap((memory) => memory.sources.map((source) => [source.id, source.characters] as const)),
  )
  const characters = [...uniqueSources.values()].reduce((sum, size) => sum + size, 0)
  const maxMemories = capability.data?.max_memories ?? 12
  const withinBudget =
    selected.length <= maxMemories &&
    uniqueSources.size <= (capability.data?.max_sources ?? 12) &&
    characters <= (capability.data?.max_source_characters ?? 16000)
  const selectionAvailable = selected.every((id) => memories.some((memory) => memory.id === id))
  const valid =
    title.trim() &&
    /^[a-z0-9][a-z0-9_-]{0,79}$/.test(slug) &&
    selected.length > 0 &&
    withinBudget &&
    selectionAvailable
  const compile = useMutation({
    mutationFn: () => {
      requestId.current ??= crypto.randomUUID()
      return wikiApi.compile(slug, title.trim(), projectId, {
        requestId: requestId.current,
        memoryIds: selected,
      })
    },
    onSuccess: (job) => {
      requestId.current = null
      setConsent(false)
      onQueued(job)
    },
  })
  const changed = () => {
    setConsent(false)
    requestId.current = null
  }
  const error = capability.error ?? sources.error ?? compile.error
  return {
    t,
    errorText,
    title,
    setTitle,
    slug,
    setSlug,
    selected,
    setSelected,
    consent,
    setConsent,
    capability,
    sources,
    memories,
    uniqueSources,
    characters,
    maxMemories,
    withinBudget,
    selectionAvailable,
    valid,
    compile,
    changed,
    error,
  }
}

export function WikiCompile(props: CompileProps) {
  const { projectId, initial, onClose, scopeName } = props
  const {
    t,
    errorText,
    title,
    setTitle,
    slug,
    setSlug,
    selected,
    setSelected,
    consent,
    setConsent,
    capability,
    sources,
    memories,
    uniqueSources,
    characters,
    maxMemories,
    withinBudget,
    selectionAvailable,
    valid,
    compile,
    changed,
    error,
  } = useCompileForm(props)
  const modelEnabled = capability.data?.enabled === true
  const submitDisabled = !valid || !consent || !modelEnabled || !!sources.error || compile.isPending
  return (
    <section className="border-hair bg-card rounded-2xl border p-5 sm:p-7" aria-label={t("compose")}>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h2 className="text-xl font-semibold">{t(initial ? "recompile" : "compose")}</h2>
        <button className={memoryButton} onClick={onClose} disabled={compile.isPending}>
          {t("close")}
        </button>
      </div>
      <p className="text-n600 mb-6 text-sm leading-6">{t("composeHint")}</p>
      <p className="text-n600 mb-4 text-sm">
        {t("compilationScope", { scope: scopeName ?? t("selectedProject") })}
      </p>
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault()
          if (!submitDisabled) compile.mutate()
        }}
      >
        <label className="flex flex-col gap-2 text-sm font-medium">
          {t("pageTitle")}
          <input
            className={memoryInput}
            value={title}
            maxLength={160}
            required
            disabled={compile.isPending}
            placeholder={t("titlePlaceholder")}
            onChange={(event) => {
              setTitle(event.target.value)
              changed()
            }}
          />
        </label>
        <fieldset disabled={compile.isPending} className="space-y-3">
          <legend className="mb-2 text-sm font-medium">{t("chooseSources")}</legend>
          <p className="text-n500 text-xs">{t(projectId ? "projectSourceScope" : "personalSourceScope")}</p>
          {sources.isPending && <p role="status">{t("loading")}</p>}
          <div className="border-hair max-h-72 space-y-1 overflow-y-auto rounded-xl border p-2">
            {memories.map((memory) => (
              <label
                key={memory.id}
                className="hover:bg-rail flex cursor-pointer items-start gap-3 rounded-lg p-3 text-sm leading-6"
              >
                <input
                  type="checkbox"
                  className="mt-1.5"
                  checked={selected.includes(memory.id)}
                  disabled={!selected.includes(memory.id) && selected.length >= maxMemories}
                  onChange={(event) => {
                    setSelected(
                      event.target.checked
                        ? [...selected, memory.id]
                        : selected.filter((id) => id !== memory.id),
                    )
                    changed()
                  }}
                />
                <span className="min-w-0">
                  <span className="block break-words">{memory.summary}</span>
                  <span className="text-n500 text-xs">
                    {t("sourceMeta", { revision: memory.revision, count: memory.source_count })}
                  </span>
                </span>
              </label>
            ))}
            {!sources.isPending && !memories.length && (
              <p className="text-n600 p-3 text-sm">
                {t("noCompileSources")}{" "}
                <Link className="text-a700 underline" to={paths.memory}>
                  {t("manageMemory")}
                </Link>
              </p>
            )}
            {sources.hasNextPage && (
              <button
                type="button"
                className={memoryButton}
                disabled={sources.isFetchingNextPage}
                onClick={() => void sources.fetchNextPage()}
              >
                {t("loadMore")}
              </button>
            )}
          </div>
          <p className={withinBudget ? "text-n500 text-xs" : "text-danger text-xs"}>
            {t("selectionBudget", {
              count: selected.length,
              max: maxMemories,
              sources: uniqueSources.size,
              characters,
            })}
          </p>
          {!withinBudget && (
            <p role="alert" className="text-danger text-sm">
              {t("overBudget")}
            </p>
          )}
          {!sources.isPending && !selectionAvailable && (
            <p role="alert" className="text-danger text-sm">
              {t("selectionUnavailable")}
            </p>
          )}
          <button
            type="button"
            className="text-a700 text-xs underline"
            onClick={() => {
              setSelected([])
              changed()
            }}
          >
            {t("clearSelection")}
          </button>
        </fieldset>
        <details className="text-n600 text-sm">
          <summary className="cursor-pointer">{t("pageAddress")}</summary>
          <label className="mt-2 flex flex-col gap-2">
            {t("slug")}
            <input
              className={memoryInput}
              value={slug}
              disabled={!!initial || compile.isPending}
              maxLength={80}
              pattern="[a-z0-9][a-z0-9_\-]{0,79}"
              onChange={(event) => {
                setSlug(event.target.value)
                changed()
              }}
            />
          </label>
        </details>
        <div className="border-hair border-t pt-4">
          <p className="text-n500 mb-3 text-xs">{t("model", { model: capability.data?.model ?? "—" })}</p>
          <label className="flex items-start gap-2 text-sm leading-6">
            <input
              type="checkbox"
              className="mt-1.5"
              checked={consent}
              disabled={!modelEnabled || compile.isPending}
              onChange={(event) => setConsent(event.target.checked)}
            />
            {t("costConsent")}
          </label>
        </div>
        {!capability.isPending && !modelEnabled && <p className="text-n600 text-sm">{t("disabled")}</p>}
        {error && (
          <p role="alert" className="text-danger text-sm">
            {errorText(error)}
          </p>
        )}
        <button type="submit" className={memoryPrimary} disabled={submitDisabled}>
          {t(compile.isPending ? "submitting" : "generate")}
        </button>
      </form>
    </section>
  )
}

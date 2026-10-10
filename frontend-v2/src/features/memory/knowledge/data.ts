import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { listMemories, memoryApi, type MemoryCleanup, type MemoryRecord } from "@/shared/api/memory"
import { useMemoryProjects, useMemoryScope } from "../api"
import { wikiApi, type WikiSummary } from "../wiki-api"
import { documentsApi, type KnowledgeDocument } from "../wiki/documents-api"
import { matches } from "./text"

export const KNOWLEDGE_VIEWS = ["overview", "memories", "topics", "files"] as const
export type KnowledgeView = (typeof KNOWLEDGE_VIEWS)[number]

/** Unknown values — including the retired management views (reviews,
 *  workflows, concepts, …) — open the overview rather than an error. */
export function readView(value: string | null): KnowledgeView {
  return (KNOWLEDGE_VIEWS as readonly string[]).includes(value ?? "") ? (value as KnowledgeView) : "overview"
}

export interface MemoryTopic {
  id: string
  title: string
  page_id: string | null
}

/** Forgotten, or reported stopped by the server: nothing of its text may show. */
export function memoryIsStopped(memory: MemoryRecord, cleanup?: MemoryCleanup) {
  return (
    memory.body_available === false ||
    memory.status === "DEPRECATED" ||
    cleanup?.stopped === true ||
    ["stopped_cleanup_pending", "cleaned"].includes(cleanup?.status ?? "")
  )
}

const PROCESSING = new Set(["pending", "parsing", "retry", "indexing"])
/** How long a page without readable text still counts as "being updated".
 *  Rebuilds finish within the maintenance cycle; a page stale for longer has
 *  usually lost everything it was built from and has nothing left to show. */
const REBUILD_WINDOW_MS = 15 * 60_000

/** `now` is when the library was last read, which keeps rendering pure; zero
 *  (nothing read yet for this search) counts nothing as rebuilding. */
function rebuilding(page: WikiSummary, now: number) {
  return now > 0 && !!page.updated_at && now - new Date(page.updated_at).getTime() < REBUILD_WINDOW_MS
}

const byRecent = (a: MemoryRecord, b: MemoryRecord) => (b.updated_at ?? "").localeCompare(a.updated_at ?? "")

/** Topic pages for a scope and search. The reader shares this cache to resolve
 *  [[links]] and related pages. Earlier results stay up while a new search
 *  loads, so typing never blanks the page. */
export function useLibrary(projectId: string, query: string) {
  const { key } = useMemoryScope()
  return useInfiniteQuery({
    queryKey: [...key, "wiki-library", projectId, query],
    queryFn: ({ pageParam }) => wikiApi.library({ projectId, query, status: "all", offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    placeholderData: (previous) => previous,
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
  })
}

/** Everything the knowledge page shows for one scope and search. Reads only:
 *  opening, searching or refreshing never calls a model or writes anything. */
export function useKnowledge(projectId: string, query: string) {
  const { key } = useMemoryScope()
  const projects = useMemoryProjects()
  const capability = useQuery({
    queryKey: [...key, "wiki-capabilities"],
    queryFn: wikiApi.capabilities,
    staleTime: 60_000,
  })
  // Searched on the server and paged, so no memory is ever out of reach.
  const memories = useInfiniteQuery({
    queryKey: [...key, "list", "active", projectId, query],
    queryFn: ({ pageParam }) => listMemories("active", projectId, { query, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    placeholderData: (previous) => previous,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })
  const processing = useQuery({
    queryKey: [...key, "processing", projectId],
    queryFn: () => memoryApi.processing(projectId),
    refetchInterval: (state) => ((state.state.data?.pending ?? 0) > 0 ? 4_000 : 30_000),
    refetchOnWindowFocus: true,
  })
  const groups = useQuery({
    queryKey: [...key, "memory-groups", projectId],
    queryFn: () =>
      http.get<{ groups: (MemoryTopic & { memory_ids: string[] })[] }>(
        "/api/memory-wiki/memory-groups" +
          (projectId ? "?" + new URLSearchParams({ project_id: projectId }) : ""),
      ),
    refetchInterval: 30_000,
  })
  const library = useLibrary(projectId, query)
  const documents = useInfiniteQuery({
    queryKey: [...key, "documents", projectId],
    queryFn: ({ pageParam }) => documentsApi.list(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    // Poll briskly only while something is still being organized.
    refetchInterval: (state) =>
      state.state.data?.pages.some((page) => page.documents.some((doc) => PROCESSING.has(doc.status)))
        ? 4_000
        : 30_000,
    refetchOnWindowFocus: true,
  })

  const allMemories = (memories.data?.pages.flatMap((page) => page.memories) ?? []).sort(byRecent)
  const docs = documents.data?.pages.flatMap((page) => page.documents) ?? []
  // A document's sections are pages too; they belong under its file, not among topics.
  const documentOf = new Map<string, KnowledgeDocument>()
  for (const doc of docs) for (const id of doc.page_ids) documentOf.set(id, doc)
  const pages = library.data?.pages.flatMap((batch) => batch.pages) ?? []
  const now = library.dataUpdatedAt
  const topics: WikiSummary[] = [
    ...pages.filter((page) => !documentOf.has(page.id) && page.body_available),
    ...pages.filter((page) => !documentOf.has(page.id) && !page.body_available && rebuilding(page, now)),
  ]
  // A search also finds a file through the text of its pages.
  const textHits = new Set(
    pages.flatMap((page) => (documentOf.has(page.id) ? [documentOf.get(page.id)!.id] : [])),
  )
  const files = docs.filter((doc) => !query || matches(doc.filename, query) || textHits.has(doc.id))
  const topicsOf = new Map<string, MemoryTopic[]>()
  for (const group of groups.data?.groups ?? [])
    for (const id of group.memory_ids) topicsOf.set(id, [...(topicsOf.get(id) ?? []), group])

  const projectName = (id: string | null | undefined) =>
    id ? (projects.data?.find((project) => project.id === id)?.name ?? null) : null

  return {
    projects,
    capability,
    memories,
    processing,
    library,
    documents,
    // Earlier results stay up while a search loads; narrow them meanwhile.
    memoryList: allMemories.filter((memory) => matches(memory.summary, query)),
    moreMemories: Boolean(memories.hasNextPage),
    topics,
    files,
    cleanupPending: documents.data?.pages[0]?.cleanup_pending ?? 0,
    topicsOf,
    projectName,
    error: memories.error ?? library.error ?? documents.error,
  }
}

export type Knowledge = ReturnType<typeof useKnowledge>

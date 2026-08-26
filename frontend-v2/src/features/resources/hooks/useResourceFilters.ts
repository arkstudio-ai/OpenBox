// The centre's filters live in the URL (§8.4): a link to "this project's
// model output, sorted by size" has to survive a refresh and be shareable.
import { useCallback, useMemo } from "react"
import { useSearchParams } from "react-router"
import { RESOURCE_KINDS } from "@/features/resources/types"
import type { KindFilter, ResourceQuery, ResourceSort, SourceFilter } from "@/features/resources/types"
import { ALL_PROJECTS } from "@/features/resources/constants"

const SOURCES: SourceFilter[] = ["all", "user", "agent"]
const SORTS: ResourceSort[] = ["created", "name", "size"]

export interface ResourceFilters extends ResourceQuery {
  /** Currently opened resource, or null. */
  selected: string | null
}

export interface ResourceFilterActions {
  setProject: (value: string) => void
  setSource: (value: SourceFilter) => void
  setKind: (value: KindFilter) => void
  setQuery: (value: string) => void
  setSort: (value: ResourceSort) => void
  select: (id: string | null) => void
}

function one<T extends string>(raw: string | null, allowed: readonly T[], fallback: T): T {
  return allowed.includes(raw as T) ? (raw as T) : fallback
}

export function useResourceFilters(defaultProject: string): [ResourceFilters, ResourceFilterActions] {
  const [params, setParams] = useSearchParams()

  const filters = useMemo<ResourceFilters>(
    () => ({
      project: params.get("project") ?? defaultProject,
      source: one(params.get("source"), SOURCES, "all"),
      kind: one(params.get("kind"), ["all", ...RESOURCE_KINDS] as KindFilter[], "all"),
      q: params.get("q") ?? "",
      sort: one(params.get("sort"), SORTS, "created"),
      selected: params.get("id"),
    }),
    [params, defaultProject],
  )

  const patch = useCallback(
    (key: string, value: string | null, keepSelection = false) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (value === null || value === "") next.delete(key)
          else next.set(key, value)
          // Changing what the list shows can hide the open resource; keeping a
          // dangling ?id= would leave the detail pane on a hidden row.
          if (!keepSelection) next.delete("id")
          return next
        },
        { replace: true },
      )
    },
    [setParams],
  )

  const actions = useMemo<ResourceFilterActions>(
    () => ({
      setProject: (value) => patch("project", value === ALL_PROJECTS ? null : value),
      setSource: (value) => patch("source", value === "all" ? null : value),
      setKind: (value) => patch("kind", value === "all" ? null : value),
      setQuery: (value) => patch("q", value),
      setSort: (value) => patch("sort", value === "created" ? null : value),
      select: (id) => patch("id", id, true),
    }),
    [patch],
  )

  return [filters, actions]
}

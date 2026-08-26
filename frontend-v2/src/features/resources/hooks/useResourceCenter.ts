// Everything the centre needs that is not layout: filters, the listing, the
// checkbox selection, and the write paths. Keeps ResourceCenter.tsx to
// composition (§6.3 — logic into a hook first).
import { useCallback, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "@/shared/ui/Toast"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import {
  useDeleteResource,
  useRenameResource,
  useResourceProjects,
  useResourcesQuery,
} from "@/features/resources/api/resources"
import { useResourceFilters } from "@/features/resources/hooks/useResourceFilters"
import { useResourceUpload } from "@/features/resources/hooks/useResourceUpload"
import { ALL_PROJECTS, NO_PROJECT } from "@/features/resources/constants"
import type { Resource } from "@/features/resources/types"

/** Rows per page. The backend caps a single request at 500. */
const PAGE = 100

export function useResourceCenter(defaultProject: string) {
  const { t } = useTranslation("resources")
  const errorMessage = useApiErrorMessage()
  const [filters, actions] = useResourceFilters(defaultProject)
  const [checked, setChecked] = useState<string[]>([])
  const [pendingDelete, setPendingDelete] = useState<Resource[] | null>(null)

  // Page size rather than an offset: React Query caches by key, so growing the
  // size re-reads one list instead of stitching pages together.
  const [pageSize, setPageSize] = useState(PAGE)
  const filterKey = `${filters.project}|${filters.source}|${filters.kind}|${filters.q}|${filters.sort}`
  const [seenFilters, setSeenFilters] = useState(filterKey)
  if (seenFilters !== filterKey) {
    setSeenFilters(filterKey)
    setPageSize(PAGE)
  }

  const projects = useResourceProjects()
  const listQuery = useResourcesQuery({ ...filters, limit: pageSize })
  const items = useMemo(() => listQuery.data?.items ?? [], [listQuery.data])

  // Uploads land in the project being browsed; the two "virtual" scopes have
  // no project to file into, so those uploads stay unfiled.
  const uploadProject =
    filters.project === ALL_PROJECTS || filters.project === NO_PROJECT ? null : filters.project
  const upload = useResourceUpload(uploadProject)

  const rename = useRenameResource()
  const remove = useDeleteResource()

  const selected = useMemo(
    () => items.find((item) => item.id === filters.selected) ?? null,
    [items, filters.selected],
  )

  const selection = useMemo(
    () => ({
      ids: checked,
      toggle: (id: string, on: boolean) =>
        setChecked((list) => (on ? [...new Set([...list, id])] : list.filter((x) => x !== id))),
      clear: () => setChecked([]),
      selectAll: () => setChecked(items.map((item) => item.id)),
    }),
    [checked, items],
  )

  const confirmDelete = useCallback(async () => {
    const targets = pendingDelete ?? []
    setPendingDelete(null)
    let failed = 0
    for (const target of targets) {
      try {
        await remove.mutateAsync(target.id)
      } catch (err) {
        failed += 1
        toast("error", errorMessage(err))
      }
    }
    const done = targets.length - failed
    if (done > 0) {
      toast("info", t("toast.deleted", { count: done }))
      setChecked((list) => list.filter((id) => !targets.some((x) => x.id === id)))
      if (targets.some((x) => x.id === filters.selected)) actions.select(null)
    }
  }, [pendingDelete, remove, errorMessage, t, filters.selected, actions])

  const mutations = useMemo(
    () => ({
      rename: (id: string, name: string) => {
        rename.mutate({ id, name }, { onError: (err) => toast("error", errorMessage(err)) })
      },
      remove: (resource: Resource) => setPendingDelete([resource]),
      removeMany: (ids: string[]) => setPendingDelete(items.filter((item) => ids.includes(item.id))),
    }),
    [rename, errorMessage, items],
  )

  return {
    filters,
    actions,
    projects: projects.data ?? [],
    list: {
      items,
      loading: listQuery.isLoading,
      total: listQuery.data?.total ?? 0,
      hasMore: listQuery.data?.hasMore ?? false,
      loadingMore: listQuery.isFetching && !listQuery.isLoading,
    },
    loadMore: () => setPageSize((n) => n + PAGE),
    selected,
    selection,
    upload,
    mutations,
    pendingDelete,
    cancelDelete: () => setPendingDelete(null),
    confirmDelete,
  }
}

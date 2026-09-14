import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate, useSearchParams } from "react-router"
import { RefreshCw } from "lucide-react"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import { useCachedFirstPage, useSessionList, useSessionListProbe } from "../api/queries"
import { useTrajectoryAccess } from "../stores/access"
import type { SessionRow } from "../types/protocol"
import {
  EMPTY_DETAIL_PARAMS,
  EMPTY_LIST_PARAMS,
  nextPage,
  parseListParams,
  previousPage,
  serializeDetailParams,
  serializeListParams,
  withFilters,
  type ListParams,
} from "../utils/params"
import { AccessNotice } from "./AccessNotice"
import { ListSortControl } from "./ListSortControl"
import { SessionFilters } from "./SessionFilters"
import { SessionTable } from "./SessionTable"

const BUTTON =
  "border-hair hover:bg-hairsoft inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs disabled:opacity-40"

function hasNewerActivity(
  newest: SessionRow | undefined,
  top: SessionRow | undefined,
  listLoaded: boolean,
): boolean {
  if (!newest) return false
  if (!top) return listLoaded
  return newest.session_id !== top.session_id || newest.last_activity_at !== top.last_activity_at
}

/** Cross-user session list. Filters, server sort, cursor and page trail live in the URL. */
export function TrajectoryListPage() {
  const { t } = useTranslation("admin-trajectories")
  const errorMessage = useApiErrorMessage()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const params = useMemo(() => parseListParams(search), [search])
  const denied = useTrajectoryAccess((s) => s.denied)
  const list = useSessionList(params)
  const firstPage = useCachedFirstPage(params)
  const probe = useSessionListProbe(params, list.isSuccess)

  if (denied) return <AccessNotice denial={denied} />

  const rows = list.data?.items ?? []
  const top = params.cursor ? firstPage?.items[0] : rows[0]
  const updated = hasNewerActivity(probe.data?.items[0], top, list.isSuccess && !params.cursor)
  const go = (next: ListParams) => setSearch(serializeListParams(next))
  const listQuery = serializeListParams(params)
  const hrefFor = (row: SessionRow) =>
    paths.adminTrajectorySession(
      row.session_id,
      serializeDetailParams({ ...EMPTY_DETAIL_PARAMS, back: listQuery }),
    )
  const refresh = () => {
    void list.refetch()
    void probe.refetch()
  }

  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-list">
      <SessionFilters
        params={params}
        onApply={(patch) => go(withFilters(params, patch))}
        onReset={() => go(EMPTY_LIST_PARAMS)}
      />
      <section className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-4">
        <div className="flex flex-wrap items-center gap-3">
          <ListSortControl value={params.sort} onChange={(sort) => go(withFilters(params, { sort }))} />
          <span className="flex-1" />
          {updated && (
            <p role="status" className="text-a700 text-xs">
              {t("list.updates")}
            </p>
          )}
          <button type="button" className={BUTTON} onClick={refresh} disabled={list.isFetching}>
            <RefreshCw size={13} aria-hidden />
            {t("list.refresh")}
          </button>
        </div>
        <SessionTable
          rows={rows}
          isLoading={list.isLoading}
          error={list.error}
          errorText={list.error ? errorMessage(list.error) : ""}
          hrefFor={hrefFor}
          onOpen={(row) => navigate(hrefFor(row))}
        />
        <nav
          aria-label={t("list.nav")}
          className="text-n600 flex flex-wrap items-center justify-end gap-2 pt-1 text-xs"
        >
          <span>{t("list.page", { page: params.trail.length + 1 })}</span>
          <button
            type="button"
            className={BUTTON}
            disabled={!params.trail.length}
            onClick={() => go(previousPage(params))}
          >
            {t("list.previous")}
          </button>
          <button
            type="button"
            className={BUTTON}
            disabled={!list.data?.has_more || !list.data.next_cursor}
            onClick={() => list.data?.next_cursor && go(nextPage(params, list.data.next_cursor))}
          >
            {t("list.next")}
          </button>
        </nav>
      </section>
    </div>
  )
}

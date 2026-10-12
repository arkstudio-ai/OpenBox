import { useMemo, useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { RefreshCw, Search } from "lucide-react"
import { Link, useNavigate, useSearchParams } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { formatNumber } from "@/shared/lib/format"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { useTrajectoryAccess } from "../../stores/access"
import { AccessNotice } from "../../components/AccessNotice"
import { useLiteUsers } from "../api"
import {
  nextPage,
  parseUsersParams,
  previousPage,
  serializeUsersParams,
  USER_WINDOWS,
  withFilters,
  type UsersParams,
} from "../params"
import type { LiteUserRow } from "../types"
import { BUTTON, Credits, FIELD, Paging, Since, Tokens, TurnStatus } from "./bits"
import { ModeToggle } from "./ModeToggle"

const WIDE = "hidden lg:table-cell"

function useColumns(hrefFor: (row: LiteUserRow) => string): DataTableColumn<LiteUserRow>[] {
  const { t } = useTranslation("admin-trajectories")
  return [
    {
      key: "user",
      header: t("lite.users.column.user"),
      className: "max-w-56",
      render: (row) => (
        <span className="flex min-w-0 flex-col">
          <Link
            to={hrefFor(row)}
            onClick={(event) => event.stopPropagation()}
            className="text-ink truncate font-medium hover:underline"
          >
            {row.user.username || row.user.email || row.user.user_id}
          </Link>
          <span className="text-n500 text-2xs block truncate">{row.user.email ?? row.user.user_id}</span>
        </span>
      ),
    },
    {
      key: "activity",
      header: t("lite.users.column.lastActivity"),
      className: "whitespace-nowrap",
      render: (row) => <Since iso={row.last_activity_at} />,
    },
    {
      key: "status",
      header: t("lite.users.column.status"),
      className: "whitespace-nowrap",
      render: (row) => <TurnStatus status={row.current_status} />,
    },
    {
      key: "turns",
      header: t("lite.users.column.turns"),
      className: "text-end whitespace-nowrap",
      render: (row) => formatNumber(row.turns),
    },
    {
      key: "failed",
      header: t("lite.users.column.failed"),
      className: "text-end whitespace-nowrap",
      render: (row) =>
        row.failed_turns ? (
          <span className="bg-dangersoft text-dangerink inline-block min-w-6 rounded-full px-2 py-0.5 text-center font-semibold">
            {formatNumber(row.failed_turns)}
          </span>
        ) : (
          <span className="text-n500">0</span>
        ),
    },
    {
      key: "sessions",
      header: t("lite.users.column.sessions"),
      className: `${WIDE} text-end whitespace-nowrap`,
      render: (row) => formatNumber(row.sessions),
    },
    {
      key: "credits",
      header: t("lite.users.column.credits"),
      className: "text-end whitespace-nowrap",
      render: (row) => <Credits value={row.credits} input={row.input_tokens} output={row.output_tokens} />,
    },
    {
      key: "usage",
      header: t("lite.users.column.usage"),
      className: `${WIDE} text-end whitespace-nowrap`,
      render: (row) => <Tokens input={row.input_tokens} output={row.output_tokens} />,
    },
    {
      key: "open",
      header: <span className="sr-only">{t("list.column.actions")}</span>,
      className: "text-end whitespace-nowrap",
      render: (row) => (
        <Link
          to={hrefFor(row)}
          onClick={(event) => event.stopPropagation()}
          className="text-a700 hover:underline"
        >
          {t("lite.users.open")}
        </Link>
      ),
    },
  ]
}

type Draft = Pick<UsersParams, "q" | "window" | "failures" | "active">

function draftOf(params: UsersParams): Draft {
  return { q: params.q, window: params.window, failures: params.failures, active: params.active }
}

function Filters({
  params,
  onApply,
  onRefresh,
  refreshing,
}: {
  params: UsersParams
  onApply: (patch: Partial<UsersParams>) => void
  onRefresh: () => void
  refreshing: boolean
}) {
  const { t } = useTranslation("admin-trajectories")
  const [draft, setDraft] = useState<Draft>(() => draftOf(params))
  const [source, setSource] = useState(params)
  if (source !== params) {
    setSource(params)
    setDraft(draftOf(params))
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onApply({ ...draft, q: draft.q.trim() })
  }
  const toggle = (patch: Partial<Draft>) => {
    const next = { ...draft, ...patch }
    setDraft(next)
    onApply({ ...next, q: next.q.trim() })
  }
  return (
    <form
      role="search"
      onSubmit={submit}
      className="border-hair bg-card flex flex-wrap items-center gap-3 rounded-xl border p-3"
    >
      <label className="relative flex min-w-0 flex-1 basis-60 items-center">
        <Search size={14} aria-hidden className="text-n500 absolute left-2.5" />
        <span className="sr-only">{t("lite.users.search")}</span>
        <input
          type="search"
          className={cn(FIELD, "w-full ps-8")}
          value={draft.q}
          onChange={(event) => setDraft({ ...draft, q: event.target.value })}
          placeholder={t("lite.users.searchPlaceholder")}
        />
      </label>
      <div
        role="group"
        aria-label={t("lite.window.label")}
        className="border-hair inline-flex overflow-hidden rounded-lg border"
      >
        {USER_WINDOWS.map((window) => (
          <button
            key={window}
            type="button"
            aria-pressed={draft.window === window}
            onClick={() => toggle({ window })}
            className={cn(
              "border-hair px-3 py-1.5 text-xs not-first:border-s first:border-s-0",
              draft.window === window ? "bg-a100 text-a700 font-medium" : "text-n600 hover:bg-hairsoft",
            )}
          >
            {t(`lite.window.${window}`)}
          </button>
        ))}
      </div>
      <label className="text-n800 flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={draft.failures}
          onChange={(event) => toggle({ failures: event.target.checked })}
        />
        {t("lite.users.onlyFailures")}
      </label>
      <label className="text-n800 flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={draft.active}
          onChange={(event) => toggle({ active: event.target.checked })}
        />
        {t("lite.users.onlyActive")}
      </label>
      <span className="flex-1" />
      <button type="submit" className="bg-ink text-bg rounded-full px-4 py-1.5 text-xs">
        {t("list.filter.apply")}
      </button>
      <button type="button" className={BUTTON} onClick={onRefresh} disabled={refreshing}>
        <RefreshCw size={13} aria-hidden />
        {t("list.refresh")}
      </button>
    </form>
  )
}

/** 简明模式首页: users with recorded turns, most recent activity first. */
export function TrajectoryUsersPage() {
  const { t } = useTranslation("admin-trajectories")
  const errorMessage = useApiErrorMessage()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const params = useMemo(() => parseUsersParams(search), [search])
  const denied = useTrajectoryAccess((s) => s.denied)
  const list = useLiteUsers(params)
  const columns = useColumns((row) => paths.adminTrajectoryUser(row.user.user_id))

  if (denied) return <AccessNotice denial={denied} />
  const rows = list.data?.items ?? []
  const go = (next: UsersParams) => setSearch(serializeUsersParams(next))

  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-lite-users">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-n600 text-sm">{t("lite.users.lead")}</p>
        <ModeToggle mode="lite" />
      </div>
      <Filters
        params={params}
        onApply={(patch) => go(withFilters(params, patch))}
        onRefresh={() => void list.refetch()}
        refreshing={list.isFetching}
      />
      <section className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-4">
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(row) => row.user.user_id}
          onRowClick={(row) => navigate(paths.adminTrajectoryUser(row.user.user_id))}
          isLoading={list.isLoading}
          error={list.error}
          emptyText={
            params.q || params.failures || params.active
              ? t("lite.users.emptyFiltered")
              : t("lite.users.empty")
          }
          errorText={list.error ? errorMessage(list.error) : ""}
          loadingLabel={t("lite.users.loading")}
          minWidth="min-w-[52rem]"
        />
        <div className="text-n500 flex flex-wrap items-center justify-between gap-2 text-xs">
          <span>{t("lite.users.footer", { window: t(`lite.window.${params.window}`) })}</span>
          <Paging
            page={params.trail.length + 1}
            hasPrevious={params.trail.length > 0}
            hasNext={!!list.data?.has_more && !!list.data.next_cursor}
            onPrevious={() => go(previousPage(params))}
            onNext={() => list.data?.next_cursor && go(nextPage(params, list.data.next_cursor))}
          />
        </div>
      </section>
    </div>
  )
}

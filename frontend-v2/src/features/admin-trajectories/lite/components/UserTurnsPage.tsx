import { Fragment, useMemo, useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { Paperclip, RefreshCw, Search } from "lucide-react"
import { Link, useNavigate, useSearchParams } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { formatNumber } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { AccessNotice } from "../../components/AccessNotice"
import { useTrajectoryAccess } from "../../stores/access"
import { EMPTY_DETAIL_PARAMS, serializeDetailParams } from "../../utils/params"
import { useLiteLabels, useLiteUser, useUserTurns, useUserTurnsProbe } from "../api"
import { turnStatusLabel, turnStatusTone } from "../labels"
import {
  isAbnormalOnly,
  nextPage,
  parseTurnsParams,
  previousPage,
  serializeTurnsParams,
  TURN_WINDOWS,
  withFilters,
  type TurnsParams,
} from "../params"
import { ABNORMAL_STATUSES, TURN_STATUSES, type TurnStatus, type TurnSummary } from "../types"
import {
  BUTTON,
  Clock,
  Duration,
  FIELD,
  Paging,
  Since,
  Tokens,
  ToolChips,
  TurnStatus as TurnStatusPill,
} from "./bits"
import { plainPreview } from "../text"
import { ModeToggle } from "./ModeToggle"

const CELL = "px-3 py-2.5 align-top"
const TONE_CHIP: Record<string, string> = {
  danger: "border-dangersoft bg-dangersoft text-dangerink",
  warn: "border-a200 bg-a200 text-n800",
  accent: "border-a100 bg-a100 text-a700",
  ok: "border-s100 bg-s100 text-s800",
  muted: "border-hair bg-card text-n700",
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "danger" | "accent" }) {
  return (
    <div className="flex flex-col">
      <span className="text-n500 text-2xs">{label}</span>
      <span
        className={cn(
          "text-lg font-semibold",
          tone === "danger" && "text-dangerink",
          tone === "accent" && "text-a700",
        )}
      >
        {value}
      </span>
    </div>
  )
}

function Filters({
  params,
  onApply,
  onRefresh,
  refreshing,
}: {
  params: TurnsParams
  onApply: (patch: Partial<TurnsParams>) => void
  onRefresh: () => void
  refreshing: boolean
}) {
  const { t } = useTranslation("admin-trajectories")
  const [q, setQ] = useState(params.q)
  const [source, setSource] = useState(params)
  if (source !== params) {
    setSource(params)
    setQ(params.q)
  }
  const abnormal = isAbnormalOnly(params.statuses)
  const toggleStatus = (status: TurnStatus) => {
    const statuses = params.statuses.includes(status)
      ? params.statuses.filter((item) => item !== status)
      : [...params.statuses, status]
    onApply({ statuses })
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onApply({ q: q.trim() })
  }
  return (
    <form
      role="search"
      onSubmit={submit}
      className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-pressed={abnormal}
          onClick={() => onApply({ statuses: abnormal ? [] : [...ABNORMAL_STATUSES] })}
          className={cn(
            "rounded-full border px-3 py-1 text-xs font-medium",
            abnormal ? "border-a700 bg-a700 text-bg" : "border-hair text-n800 hover:bg-hairsoft",
          )}
        >
          {t("lite.turns.abnormalOnly")}
        </button>
        <span className="bg-hair mx-1 h-4 w-px" aria-hidden />
        <div role="group" aria-label={t("lite.turns.statusFilter")} className="flex flex-wrap gap-1.5">
          {TURN_STATUSES.map((status) => {
            const on = params.statuses.includes(status)
            return (
              <button
                key={status}
                type="button"
                aria-pressed={on}
                onClick={() => toggleStatus(status)}
                className={cn(
                  "rounded-full border px-2.5 py-0.5 text-xs",
                  on ? TONE_CHIP[turnStatusTone(status)] : "border-hair text-n600 hover:bg-hairsoft",
                )}
              >
                {turnStatusLabel(status, t)}
              </button>
            )
          })}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-n600 flex items-center gap-2 text-xs">
          {t("lite.window.label")}
          <select
            className={cn(FIELD, "py-1 text-xs")}
            value={params.window}
            onChange={(event) => onApply({ window: event.target.value as TurnsParams["window"] })}
          >
            {TURN_WINDOWS.map((window) => (
              <option key={window} value={window}>
                {t(`lite.window.${window}`)}
              </option>
            ))}
          </select>
        </label>
        {params.session && (
          <button type="button" className={BUTTON} onClick={() => onApply({ session: "" })}>
            {t("lite.turns.clearSession")}
          </button>
        )}
        <label className="relative flex min-w-0 flex-1 basis-52 items-center">
          <Search size={14} aria-hidden className="text-n500 absolute left-2.5" />
          <span className="sr-only">{t("lite.turns.search")}</span>
          <input
            type="search"
            className={cn(FIELD, "w-full ps-8 text-xs")}
            value={q}
            onChange={(event) => setQ(event.target.value)}
            placeholder={t("lite.turns.searchPlaceholder")}
          />
        </label>
        <button type="submit" className="bg-ink text-bg rounded-full px-4 py-1.5 text-xs">
          {t("list.filter.apply")}
        </button>
        <button type="button" className={BUTTON} onClick={onRefresh} disabled={refreshing}>
          <RefreshCw size={13} aria-hidden />
          {t("list.refresh")}
        </button>
      </div>
    </form>
  )
}

function Preview({ text, attachments }: { text: string | null; attachments?: boolean }) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <span className="line-clamp-2 break-words">
      {plainPreview(text) || <span className="text-n500">{t("common.notRecorded")}</span>}
      {attachments && (
        <span className="bg-hairsoft text-n600 text-2xs ms-1 inline-flex items-center gap-1 rounded px-1.5 align-middle">
          <Paperclip size={10} aria-hidden />
          {t("lite.turns.attachments")}
        </span>
      )}
    </span>
  )
}

/** Rows grouped by session: a heading row before each run of turns of the same session. */
function TurnsTable({
  rows,
  userId,
  backQuery,
  onSession,
}: {
  rows: TurnSummary[]
  userId: string
  backQuery: string
  onSession: (sessionId: string) => void
}) {
  const { t } = useTranslation("admin-trajectories")
  const labels = useLiteLabels()
  const navigate = useNavigate()
  const hrefFor = (row: TurnSummary) =>
    paths.adminTrajectoryTurn(
      userId,
      row.session_id,
      row.turn_id,
      backQuery ? new URLSearchParams({ back: backQuery }).toString() : undefined,
    )
  const proHref = (sessionId: string) =>
    paths.adminTrajectorySession(sessionId, serializeDetailParams(EMPTY_DETAIL_PARAMS))
  const groups: Array<{ sessionId: string; title: string | null; rows: TurnSummary[] }> = []
  for (const row of rows) {
    const last = groups[groups.length - 1]
    if (last && last.sessionId === row.session_id) last.rows.push(row)
    else groups.push({ sessionId: row.session_id, title: row.session_title, rows: [row] })
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[64rem] text-start text-xs">
        <thead className="text-n500">
          <tr>
            <th className={`${CELL} w-24 font-medium`}>{t("lite.turns.column.time")}</th>
            <th className={`${CELL} font-medium`}>{t("lite.turns.column.userSaid")}</th>
            <th className={`${CELL} w-60 font-medium`}>{t("lite.turns.column.aiDid")}</th>
            <th className={`${CELL} font-medium`}>{t("lite.turns.column.aiSaid")}</th>
            <th className={`${CELL} w-32 font-medium`}>{t("lite.turns.column.result")}</th>
            <th className={`${CELL} w-20 text-end font-medium`}>{t("lite.turns.column.duration")}</th>
            <th className={`${CELL} w-16 text-end font-medium`}>{t("lite.turns.column.usage")}</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <Fragment key={`${group.sessionId}:${group.rows[0]!.turn_id}`}>
              <tr className="bg-surface">
                <th colSpan={7} scope="colgroup" className="px-3 py-1.5 text-start font-normal">
                  <span className="flex flex-wrap items-center gap-3">
                    <span className="text-ink font-medium">{group.title || t("list.untitled")}</span>
                    <span className="text-n500">
                      {t("lite.turns.groupCount", { count: group.rows.length })}
                    </span>
                    <button
                      type="button"
                      className="text-a700 hover:underline"
                      onClick={() => onSession(group.sessionId)}
                    >
                      {t("lite.turns.onlySession")}
                    </button>
                    <Link to={proHref(group.sessionId)} className="text-a700 hover:underline">
                      {t("lite.turns.openPro")}
                    </Link>
                  </span>
                </th>
              </tr>
              {group.rows.map((row) => (
                <tr
                  key={row.turn_id}
                  tabIndex={0}
                  onClick={() => navigate(hrefFor(row))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") navigate(hrefFor(row))
                  }}
                  data-status={row.status}
                  className={cn(
                    "border-hair hover:bg-hairsoft cursor-pointer border-t",
                    row.status === "failed" && "bg-dangersoft/40",
                    row.status === "waiting" && "bg-a100/40",
                  )}
                >
                  <td className={`${CELL} text-n700 whitespace-nowrap`}>
                    <Clock iso={row.started_at} />
                  </td>
                  <td className={CELL}>
                    <Link
                      to={hrefFor(row)}
                      onClick={(event) => event.stopPropagation()}
                      className="text-ink hover:underline"
                    >
                      <Preview text={row.user_preview} attachments={row.has_attachments} />
                    </Link>
                  </td>
                  <td className={CELL}>
                    <ToolChips counts={row.tool_counts} labels={labels} />
                  </td>
                  <td className={`${CELL} text-n700`}>
                    <Preview text={row.assistant_preview} />
                  </td>
                  <td className={`${CELL} whitespace-nowrap`}>
                    <TurnStatusPill status={row.status} reason={row.status_reason} />
                  </td>
                  <td className={`${CELL} text-n700 text-end whitespace-nowrap`}>
                    <Duration ms={row.duration_ms} />
                  </td>
                  <td className={`${CELL} text-n500 text-end whitespace-nowrap`}>
                    <Tokens input={row.input_tokens} output={row.output_tokens} compact />
                  </td>
                </tr>
              ))}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function UserCard({
  userId,
  name,
  profile,
}: {
  userId: string
  name: string
  profile: ReturnType<typeof useLiteUser>
}) {
  const { t } = useTranslation("admin-trajectories")
  const user = profile.user
  return (
    <section className="border-hair bg-card flex flex-wrap items-start justify-between gap-4 rounded-xl border p-4">
      <div className="flex min-w-0 items-center gap-3">
        <div className="bg-a100 text-a700 flex size-10 flex-none items-center justify-center rounded-full text-base font-semibold">
          {name.slice(0, 1).toUpperCase()}
        </div>
        <div className="flex min-w-0 flex-col">
          <h2 className="text-ink truncate text-base font-semibold">{name}</h2>
          <p className="text-n500 truncate text-xs">
            {user?.user.email ?? userId}
            {user?.last_activity_at && (
              <>
                {" · "}
                {t("lite.users.column.lastActivity")} <Since iso={user.last_activity_at} />
              </>
            )}
          </p>
        </div>
      </div>
      {user ? (
        <div className="flex flex-wrap gap-6">
          <Stat label={t("lite.user.turns7d")} value={formatNumber(user.turns)} />
          <Stat
            label={t("lite.turnStatus.failed")}
            value={formatNumber(user.failed_turns)}
            tone={user.failed_turns ? "danger" : undefined}
          />
          <Stat
            label={t("lite.turnStatus.waiting")}
            value={formatNumber(user.waiting_turns)}
            tone={user.waiting_turns ? "accent" : undefined}
          />
          <Stat label={t("lite.users.column.sessions")} value={formatNumber(user.sessions)} />
        </div>
      ) : profile.isLoading ? (
        <Spinner className="size-4" />
      ) : (
        <p className="text-n500 text-xs">{t("lite.user.noRecentActivity")}</p>
      )}
    </section>
  )
}

function ListBody({
  list,
  params,
  userId,
  backQuery,
  onSession,
}: {
  list: ReturnType<typeof useUserTurns>
  params: TurnsParams
  userId: string
  backQuery: string
  onSession: (sessionId: string) => void
}) {
  const { t } = useTranslation("admin-trajectories")
  const errorMessage = useApiErrorMessage()
  if (list.isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Spinner className="size-5" />
        <span className="sr-only">{t("lite.turns.loading")}</span>
      </div>
    )
  }
  if (list.error) {
    return (
      <p role="alert" className="text-danger py-10 text-center text-sm">
        {errorMessage(list.error)}
      </p>
    )
  }
  const rows = list.data?.items ?? []
  if (rows.length === 0) {
    const filtered = params.statuses.length || params.q || params.session
    return (
      <p className="text-n600 py-10 text-center text-sm">
        {filtered ? t("lite.turns.emptyFiltered") : t("lite.turns.empty")}
      </p>
    )
  }
  return <TurnsTable rows={rows} userId={userId} backQuery={backQuery} onSession={onSession} />
}

function hasNewer(newest: TurnSummary | undefined, top: TurnSummary | undefined, listed: boolean): boolean {
  if (!newest || !listed) return false
  return !top || newest.turn_id !== top.turn_id || newest.last_activity_at !== top.last_activity_at
}

/** 某用户的对话轮: every turn of one user across sessions, newest first, abnormal ones one switch away. */
export function TrajectoryUserTurnsPage({ userId }: { userId: string }) {
  const { t } = useTranslation("admin-trajectories")
  const [search, setSearch] = useSearchParams()
  const params = useMemo(() => parseTurnsParams(search), [search])
  const denied = useTrajectoryAccess((s) => s.denied)
  const list = useUserTurns(userId, params)
  const profile = useLiteUser(userId)
  const probe = useUserTurnsProbe(userId, params, list.isSuccess)
  if (denied) return <AccessNotice denial={denied} />

  const rows = list.data?.items ?? []
  const updated = !params.cursor && hasNewer(probe.data?.items[0], rows[0], list.isSuccess)
  const go = (next: TurnsParams) => setSearch(serializeTurnsParams(next))
  const refresh = () => {
    void list.refetch()
    void probe.refetch()
  }
  const name = profile.user?.user.username || profile.user?.user.email || userId
  const backQuery = serializeTurnsParams(params)

  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-lite-turns">
      <nav aria-label={t("lite.breadcrumb")} className="text-n500 flex flex-wrap items-center gap-2 text-xs">
        <Link to={paths.adminTrajectories()} className="hover:underline">
          {t("lite.users.title")}
        </Link>
        <span aria-hidden>/</span>
        <span className="text-ink">{name}</span>
        <span className="flex-1" />
        <ModeToggle mode="lite" />
      </nav>
      <UserCard userId={userId} name={name} profile={profile} />
      <Filters
        params={params}
        onApply={(patch) => go(withFilters(params, patch))}
        onRefresh={refresh}
        refreshing={list.isFetching}
      />
      {updated && (
        <button
          type="button"
          onClick={refresh}
          className="bg-a100 text-a700 border-a100 rounded-lg border px-3 py-2 text-xs"
          role="status"
        >
          {t("lite.turns.updates")}
        </button>
      )}
      <section className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-2">
        <ListBody
          list={list}
          params={params}
          userId={userId}
          backQuery={backQuery}
          onSession={(session) => go(withFilters(params, { session }))}
        />
        <div className="text-n500 flex flex-wrap items-center justify-between gap-2 px-2 text-xs">
          <span>{t("lite.turns.footer", { count: rows.length })}</span>
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

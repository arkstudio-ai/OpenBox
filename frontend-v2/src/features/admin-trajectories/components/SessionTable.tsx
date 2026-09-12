import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { formatTokens } from "@/shared/lib/format"
import type { SessionRow } from "../types/protocol"
import { formatInstant } from "../utils/time"
import { Status } from "./StatusPills"

interface Props {
  rows: readonly SessionRow[]
  isLoading: boolean
  error: unknown
  errorText: string
  /** Detail link for a row, already carrying the return route. */
  hrefFor: (row: SessionRow) => string
  onOpen: (row: SessionRow) => void
}

const SECONDARY = "text-n500 block truncate font-mono text-2xs"
const WIDE = "hidden lg:table-cell"
const WIDER = "hidden xl:table-cell"

function Stack({ primary, secondary }: { primary: string; secondary: string | null }) {
  return (
    <span className="flex min-w-0 flex-col">
      <span className="text-ink truncate">{primary}</span>
      {secondary && <span className={SECONDARY}>{secondary}</span>}
    </span>
  )
}

function useColumns(hrefFor: Props["hrefFor"]): DataTableColumn<SessionRow>[] {
  const { t, i18n } = useTranslation("admin-trajectories")
  const when = (iso: string | null) => formatInstant(iso, "local", i18n.language) ?? t("common.notRecorded")
  const counters = (row: SessionRow) => row.statistics
  return [
    {
      key: "user",
      header: t("list.column.user"),
      className: "max-w-48",
      render: (row) => (
        <Stack primary={row.owner.username ?? row.owner.email ?? row.user_id} secondary={row.user_id} />
      ),
    },
    {
      key: "session",
      header: t("list.column.session"),
      className: "max-w-64",
      render: (row) => (
        <span className="flex min-w-0 flex-col">
          <Link
            to={hrefFor(row)}
            onClick={(event) => event.stopPropagation()}
            className="text-ink truncate font-medium hover:underline"
          >
            {row.title || t("list.untitled")}
          </Link>
          <span className={SECONDARY}>{row.session_id}</span>
        </span>
      ),
    },
    {
      key: "workspace",
      header: t("list.column.workspace"),
      className: `${WIDE} max-w-40`,
      render: (row) => (
        <Stack primary={row.workspace?.name ?? t("list.noWorkspaceName")} secondary={row.workspace_id} />
      ),
    },
    {
      key: "run",
      header: t("list.column.runStatus"),
      className: "whitespace-nowrap",
      render: (row) => <Status scope="run" value={row.running_status} />,
    },
    {
      key: "recording",
      header: t("list.column.recording"),
      className: "whitespace-nowrap",
      render: (row) => <Status scope="recording" value={row.recording_status} />,
    },
    {
      key: "activity",
      header: t("list.column.lastActivity"),
      className: "whitespace-nowrap",
      render: (row) => when(row.last_activity_at),
    },
    {
      key: "coverage",
      header: t("list.column.coverage"),
      className: `${WIDER} whitespace-nowrap`,
      render: (row) => (row.trajectory_id ? when(row.coverage_start) : t("list.notStarted")),
    },
    {
      key: "model",
      header: t("list.column.modelAgent"),
      className: `${WIDER} max-w-40`,
      render: (row) => <Stack primary={row.model ?? t("common.notRecorded")} secondary={row.agent} />,
    },
    {
      key: "counts",
      header: t("list.column.activity"),
      className: `${WIDE} whitespace-nowrap`,
      render: (row) => {
        const stats = counters(row)
        if (!row.trajectory_id || !stats) return t("common.dash")
        return (
          <span className="flex flex-col">
            <span>
              {t("list.counts", { requests: stats.request_count ?? 0, tools: stats.tool_count ?? 0 })}
            </span>
            <span className="text-n500 text-2xs">
              {t("list.failures", { errors: stats.error_count ?? 0, unknown: stats.unknown_count ?? 0 })}
            </span>
          </span>
        )
      },
    },
    {
      key: "usage",
      header: t("list.column.usage"),
      className: `${WIDER} whitespace-nowrap`,
      render: (row) => {
        const stats = counters(row)
        if (!row.trajectory_id || !stats) return t("common.dash")
        if (stats.input_tokens == null || stats.output_tokens == null) return t("common.notRecorded")
        return t("list.tokens", {
          input: formatTokens(stats.input_tokens),
          output: formatTokens(stats.output_tokens),
        })
      },
    },
    {
      key: "open",
      header: <span className="sr-only">{t("list.column.actions")}</span>,
      render: (row) => (
        <Link
          to={hrefFor(row)}
          onClick={(event) => event.stopPropagation()}
          className="text-a700 whitespace-nowrap hover:underline"
        >
          {t("list.open")}
        </Link>
      ),
    },
  ]
}

/** Summaries only: the list never loads prompts or events. */
export function SessionTable({ rows, isLoading, error, errorText, hrefFor, onOpen }: Props) {
  const { t } = useTranslation("admin-trajectories")
  const columns = useColumns(hrefFor)
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(row) => row.session_id}
      onRowClick={onOpen}
      isLoading={isLoading}
      error={error}
      emptyText={t("list.empty")}
      errorText={errorText}
      loadingLabel={t("list.loading")}
      // Nine dense columns (two full timestamps among them). Without a floor the
      // table is just `w-full` and spreads its columns to whatever width it is
      // handed; with one it keeps its natural rhythm and scrolls in its card.
      minWidth="min-w-[72rem]"
    />
  )
}

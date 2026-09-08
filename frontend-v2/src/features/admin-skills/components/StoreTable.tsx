import { useTranslation } from "react-i18next"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { formatDateTime, formatNumber } from "@/shared/lib/format"
import { listingTone } from "@/features/admin-skills/lib/entry"
import type { StoreEntry } from "@/features/admin-skills/types"
import { StoreActions } from "./StoreActions"
import { StoreEntryCell } from "./StoreEntryCell"

interface Props {
  rows: readonly StoreEntry[]
  isLoading: boolean
  error: unknown
  /** The row a write is in flight for; its buttons stay disabled until it lands. */
  busyId: string | null
  onInstalls: (entry: StoreEntry) => void
  onRelist: (entry: StoreEntry) => void
  onDelist: (entry: StoreEntry) => void
  onFeature: (entry: StoreEntry) => void
  onOfficial: (entry: StoreEntry) => void
  onView: (entry: StoreEntry) => void
}

export function StoreTable({ rows, isLoading, error, busyId, onInstalls, ...actions }: Props) {
  const { t } = useTranslation("admin-skills")

  const columns: DataTableColumn<StoreEntry>[] = [
    {
      key: "entry",
      header: t("store.column.entry"),
      className: "min-w-[16rem]",
      render: (entry) => <StoreEntryCell entry={entry} />,
    },
    {
      key: "origin",
      header: t("store.column.origin"),
      render: (entry) => <StatusPill>{t(`store.origin.${entry.origin}`)}</StatusPill>,
    },
    {
      key: "author",
      header: t("store.column.author"),
      // The email is the way to reach a submitter, but a column of addresses
      // is unreadable — it hides behind the username instead.
      render: (entry) =>
        entry.author ? (
          <span title={entry.author.email}>{entry.author.username}</span>
        ) : (
          <span className="text-n600">{entry.publisher || "—"}</span>
        ),
    },
    {
      key: "version",
      header: t("store.column.version"),
      className: "font-mono",
      render: (entry) => (entry.version == null ? "—" : String(entry.version)),
    },
    {
      key: "published_at",
      header: t("store.column.publishedAt"),
      render: (entry) =>
        entry.published_at ? (
          <time dateTime={entry.published_at}>{formatDateTime(entry.published_at)}</time>
        ) : (
          "—"
        ),
    },
    {
      key: "installs_count",
      header: t("store.column.installs"),
      render: (entry) => (
        <button
          type="button"
          title={t("store.installsOf", { title: entry.title })}
          onClick={() => onInstalls(entry)}
          className="text-a700 rounded px-1 hover:underline"
        >
          {formatNumber(entry.installs_count ?? 0)}
        </button>
      ),
    },
    {
      key: "listing",
      header: t("store.column.status"),
      render: (entry) => (
        <StatusPill tone={listingTone(entry.listing)} title={entry.listing_note ?? undefined}>
          {t(`status.${entry.listing}`)}
        </StatusPill>
      ),
    },
    {
      key: "actions",
      header: <span className="sr-only">{t("store.column.actions")}</span>,
      className: "text-end",
      render: (entry) => <StoreActions entry={entry} busy={busyId === entry.catalog_id} {...actions} />,
    },
  ]

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(entry) => entry.catalog_id}
      isLoading={isLoading}
      error={error}
      minWidth="min-w-[70rem]"
      emptyText={t("store.empty")}
      errorText={t("list.error")}
      loadingLabel={t("list.loading")}
    />
  )
}

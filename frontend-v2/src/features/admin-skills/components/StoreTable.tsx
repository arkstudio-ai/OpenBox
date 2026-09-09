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
  onEdit: (entry: StoreEntry) => void
  onDelete: (entry: StoreEntry) => void
  onRestore: (entry: StoreEntry) => void
  selected: Set<string>
  onSelect: (id: string) => void
  disabled?: boolean
}

export function StoreTable({
  rows,
  isLoading,
  error,
  busyId,
  onInstalls,
  selected,
  onSelect,
  disabled,
  ...actions
}: Props) {
  const { t } = useTranslation("admin-skills")

  const columns: DataTableColumn<StoreEntry>[] = [
    {
      key: "entry",
      header: t("store.column.entry"),
      className: "min-w-0 max-w-96",
      render: (entry) => (
        <div className="flex min-w-0 gap-2">
          {!entry.deleted && (
            <input
              type="checkbox"
              aria-label={t("manage.select", { title: entry.title })}
              checked={selected.has(entry.catalog_id)}
              disabled={disabled}
              onChange={() => onSelect(entry.catalog_id)}
              className="mt-1 size-4 shrink-0"
            />
          )}
          <div className="min-w-0 flex-1">
            <StoreEntryCell entry={entry} />
            <div className="mt-2 flex flex-wrap items-center gap-2 sm:hidden">
              <button
                type="button"
                title={t("store.installsOf", { title: entry.title })}
                onClick={() => onInstalls(entry)}
                className="text-a700 rounded px-1 hover:underline"
              >
                {t("store.column.installs")}: {formatNumber(entry.installs_count ?? 0)}
              </button>
              <StatusPill tone={listingTone(entry.listing)} title={entry.listing_note ?? undefined}>
                {entry.deleted ? t("manage.deleted") : t(`status.${entry.listing}`)}
              </StatusPill>
            </div>
            <div className="mt-3">
              <StoreActions entry={entry} busy={!!disabled || busyId === entry.catalog_id} {...actions} />
            </div>
          </div>
        </div>
      ),
    },
    {
      key: "origin",
      className: "hidden lg:table-cell",
      header: t("store.column.origin"),
      render: (entry) => <StatusPill>{t(`store.origin.${entry.origin}`)}</StatusPill>,
    },
    {
      key: "author",
      className: "hidden xl:table-cell",
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
      className: "hidden font-mono lg:table-cell",
      render: (entry) => (entry.version == null ? "—" : String(entry.version)),
    },
    {
      key: "published_at",
      className: "hidden 2xl:table-cell",
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
      className: "hidden whitespace-nowrap sm:table-cell",
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
      className: "hidden whitespace-nowrap sm:table-cell",
      header: t("store.column.status"),
      render: (entry) => (
        <StatusPill tone={listingTone(entry.listing)} title={entry.listing_note ?? undefined}>
          {entry.deleted ? t("manage.deleted") : t(`status.${entry.listing}`)}
        </StatusPill>
      ),
    },
  ]

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(entry) => entry.catalog_id}
      isLoading={isLoading}
      error={error}
      minWidth="w-full table-fixed sm:table-auto"
      emptyText={t("store.empty")}
      errorText={t("list.error")}
      loadingLabel={t("list.loading")}
    />
  )
}

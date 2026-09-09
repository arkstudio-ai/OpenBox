import { useTranslation } from "react-i18next"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { DisplayIcon } from "@/shared/ui/DisplayIcon"
import { formatDateTime } from "@/shared/lib/format"
import type { InstallRecord } from "@/features/admin-skills/types"

interface Props {
  rows: readonly InstallRecord[]
  isLoading: boolean
  error: unknown
}

/** Who has what, straight from `skill_installs`. */
export function InstallsTable({ rows, isLoading, error }: Props) {
  const { t } = useTranslation("admin-skills")

  const columns: DataTableColumn<InstallRecord>[] = [
    {
      key: "user",
      header: t("installs.column.user"),
      render: (row) => (
        <div className="min-w-0">
          <div>{row.user?.username || "—"}</div>
          <div className="text-n600 text-2xs truncate">{row.user?.email}</div>
        </div>
      ),
    },
    {
      key: "skill",
      header: t("installs.column.skill"),
      className: "min-w-[16rem]",
      render: (row) => (
        <div className="flex items-start gap-2">
          <DisplayIcon icon={row.icon} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <span>{row.title}</span>
              {/* Only present when the server could resolve it; a catalogue
                  entry's origin is not derivable from its id alone. */}
              {row.origin && <StatusPill>{t(`store.origin.${row.origin}`)}</StatusPill>}
            </div>
            <div className="text-n600 text-2xs truncate font-mono">{row.catalog_id}</div>
          </div>
        </div>
      ),
    },
    {
      key: "kind",
      header: t("installs.column.kind"),
      render: (row) => t(`store.kind.${row.kind}`),
    },
    {
      key: "install_dir",
      header: t("installs.column.installDir"),
      className: "font-mono break-all",
    },
    {
      key: "installed_at",
      header: t("installs.column.installedAt"),
      render: (row) =>
        row.installed_at ? <time dateTime={row.installed_at}>{formatDateTime(row.installed_at)}</time> : "—",
    },
  ]

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(row) => row.id}
      isLoading={isLoading}
      error={error}
      minWidth="min-w-[56rem]"
      emptyText={t("installs.empty")}
      errorText={t("list.error")}
      loadingLabel={t("list.loading")}
    />
  )
}

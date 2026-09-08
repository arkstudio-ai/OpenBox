// Column set for the order list. Shared with the workspace detail page, which
// drops the workspace column because the whole card is already one workspace.
import { useTranslation } from "react-i18next"
import { formatCredits } from "@/shared/lib/format"
import type { DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { DASH, formatWhen } from "@/features/admin-billing/lib/display"
import { formatFen } from "@/features/admin-billing/lib/money"
import { orderTone } from "@/features/admin-billing/lib/status"
import type { AdminOrder, AdminOrderRow } from "@/features/admin-billing/types"
import { UserCell } from "./UserCell"
import { WorkspaceCell } from "./WorkspaceCell"

export function useOrderColumns({
  showWorkspace = false,
  providerName,
}: {
  showWorkspace?: boolean
  /** Resolves a provider id to its display name; falls back to the id. */
  providerName?: (id: string) => string
} = {}): DataTableColumn<AdminOrderRow>[] {
  const { t, i18n } = useTranslation("admin-billing")
  const column = (key: string) => t(`orders.columns.${key}`)
  const product = (row: AdminOrder) =>
    row.kind === "subscription"
      ? [
          t(`plans.${row.plan_id}`, { defaultValue: row.plan_id ?? DASH }),
          row.cycle ? t(`cycles.${row.cycle}`, { defaultValue: row.cycle }) : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : t("kinds.topup")

  const columns: DataTableColumn<AdminOrderRow>[] = [
    {
      key: "id",
      header: column("id"),
      className: "max-w-[13rem] font-mono text-2xs",
      render: (row) => <span className="block truncate">{row.id}</span>,
    },
    {
      key: "created_at",
      header: column("createdAt"),
      className: "whitespace-nowrap",
      render: (row) => formatWhen(row.created_at),
    },
  ]
  if (showWorkspace) {
    columns.push({
      key: "workspace",
      header: column("workspace"),
      className: "max-w-[14rem]",
      render: (row) => <WorkspaceCell name={row.workspace_name ?? null} id={row.workspace_id} />,
    })
  }
  columns.push(
    {
      key: "user",
      header: column("user"),
      className: "max-w-[12rem]",
      render: (row) => <UserCell user={row.user} />,
    },
    { key: "product", header: column("product"), render: product },
    {
      key: "amount",
      header: column("amount"),
      className: "whitespace-nowrap tabular-nums",
      render: (row) => formatFen(row.amount_fen, row.currency, i18n.language),
    },
    {
      key: "credits",
      header: column("credits"),
      className: "tabular-nums",
      render: (row) => formatCredits(row.credits),
    },
    {
      key: "provider",
      header: column("provider"),
      render: (row) => providerName?.(row.provider) ?? row.provider,
    },
    {
      key: "status",
      header: column("status"),
      render: (row) => (
        <StatusPill
          tone={orderTone(row.status)}
          // A cancelled order without its reason sends the operator to the DB.
          title={
            row.cancellation_reason
              ? t("orders.cancelled", { reason: row.cancellation_reason })
              : undefined
          }
        >
          {t(`orderStatus.${row.status}`)}
        </StatusPill>
      ),
    },
    {
      key: "paid_at",
      header: column("paidAt"),
      className: "whitespace-nowrap",
      render: (row) => formatWhen(row.paid_at),
    },
    {
      key: "provider_order_id",
      header: column("providerOrderId"),
      className: "max-w-[13rem] font-mono text-2xs",
      render: (row) => <span className="block truncate">{row.provider_order_id || DASH}</span>,
    },
  )
  return columns
}

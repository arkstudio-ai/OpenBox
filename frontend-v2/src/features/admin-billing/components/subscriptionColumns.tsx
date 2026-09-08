// Column set for the subscription list, kept beside the page so the page file
// stays about state and this one about presentation.
import { useTranslation } from "react-i18next"
import { formatCredits, formatNumber } from "@/shared/lib/format"
import type { DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { DASH, formatWhen } from "@/features/admin-billing/lib/display"
import { subscriptionTone } from "@/features/admin-billing/lib/status"
import type { SubscriptionRow } from "@/features/admin-billing/types"
import { UserCell } from "./UserCell"
import { WorkspaceCell } from "./WorkspaceCell"

export function useSubscriptionColumns(): DataTableColumn<SubscriptionRow>[] {
  const { t } = useTranslation("admin-billing")
  const column = (key: string) => t(`subscriptions.columns.${key}`)
  return [
    {
      key: "workspace",
      header: column("workspace"),
      className: "max-w-[16rem]",
      render: (row) => <WorkspaceCell name={row.workspace.name} id={row.workspace.id} />,
    },
    {
      key: "owner",
      header: column("owner"),
      className: "max-w-[14rem]",
      render: (row) => <UserCell user={row.owner} />,
    },
    {
      key: "plan",
      header: column("plan"),
      // An unknown id is shown raw rather than as a missing-translation key:
      // the plan catalogue can gain an entry before the locales catch up.
      render: (row) => t(`plans.${row.plan_id}`, { defaultValue: row.plan_id }),
    },
    {
      key: "cycle",
      header: column("cycle"),
      render: (row) => (row.cycle ? t(`cycles.${row.cycle}`, { defaultValue: row.cycle }) : DASH),
    },
    {
      key: "term",
      header: column("term"),
      render: (row) => (
        <div className="flex flex-col whitespace-nowrap text-n700">
          <span>{formatWhen(row.starts_at)}</span>
          <span className="text-n500">{formatWhen(row.ends_at)}</span>
        </div>
      ),
    },
    {
      key: "state",
      header: column("state"),
      render: (row) => (
        <StatusPill tone={subscriptionTone(row.state)}>{t(`states.${row.state}`)}</StatusPill>
      ),
    },
    {
      key: "balance",
      header: column("balance"),
      className: "tabular-nums",
      render: (row) => formatCredits(row.balance),
    },
    {
      key: "queued",
      header: column("queued"),
      className: "tabular-nums",
      render: (row) => formatNumber(row.queued_count),
    },
    {
      key: "lastPaid",
      header: column("lastPaid"),
      className: "whitespace-nowrap",
      render: (row) => formatWhen(row.last_paid_at),
    },
  ]
}

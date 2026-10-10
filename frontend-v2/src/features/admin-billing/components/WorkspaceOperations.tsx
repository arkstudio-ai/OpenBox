import { useTranslation } from "react-i18next"
import { DataTable } from "@/shared/ui/DataTable"
import { formatCredits } from "@/shared/lib/format"
import type { BillingOperation } from "@/features/admin-billing/types"
import { formatWhen } from "@/features/admin-billing/lib/display"
import { SectionCard } from "./SectionCard"

export function WorkspaceOperations({ operations }: { operations: BillingOperation[] }) {
  const { t } = useTranslation("admin-billing")
  return (
    <SectionCard title={t("operations.title")} hint={t("operations.hint")}>
      <DataTable<BillingOperation>
        columns={[
          { key: "created_at", header: t("operations.time"), render: (row) => formatWhen(row.created_at) },
          { key: "actor", header: t("operations.actor"), render: (row) => row.actor?.username ?? "—" },
          {
            key: "action",
            header: t("actions.manage"),
            render: (row) => t(`operations.${row.action}`, { defaultValue: row.action }),
          },
          {
            key: "result",
            header: t("operations.result"),
            render: (row) => (
              <div className="space-y-1 text-xs">
                <p>
                  {formatCredits(row.before_balance)} → {formatCredits(row.balance)}
                </p>
                {row.subscription && (
                  <p>
                    {t(`plans.${row.subscription.plan_id}`)} ·{" "}
                    {row.subscription.cancelled_at
                      ? t("workspace.terms.cancelled")
                      : formatWhen(row.subscription.ends_at)}
                  </p>
                )}
              </div>
            ),
          },
          {
            key: "reason",
            header: t("actions.reason"),
            className: "max-w-[20rem] whitespace-normal break-words",
          },
        ]}
        rows={operations}
        rowKey={(row) => row.id}
        emptyText={t("operations.empty")}
        errorText={t("list.error")}
        loadingLabel={t("list.loading")}
        minWidth="min-w-[48rem]"
      />
    </SectionCard>
  )
}

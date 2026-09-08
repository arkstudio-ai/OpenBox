// The workspace's own order tail, with the same columns as the global list
// minus the workspace itself.
import { useTranslation } from "react-i18next"
import { usePaymentProviders } from "@/shared/api/billing"
import { formatNumber } from "@/shared/lib/format"
import { DataTable } from "@/shared/ui/DataTable"
import type { AdminOrder } from "@/features/admin-billing/types"
import { SectionCard } from "./SectionCard"
import { useOrderColumns } from "./orderColumns"

export function WorkspaceOrders({ orders, limit }: { orders: AdminOrder[]; limit: number }) {
  const { t } = useTranslation("admin-billing")
  const providers = usePaymentProviders()
  const columns = useOrderColumns({
    providerName: (id) => providers.data?.items.find((item) => item.id === id)?.name ?? id,
  })

  return (
    <SectionCard
      title={t("workspace.orders.title")}
      hint={t("workspace.orders.hint", { value: formatNumber(limit) })}
    >
      <DataTable
        columns={columns}
        rows={orders}
        rowKey={(order) => order.id}
        emptyText={t("workspace.orders.empty")}
        errorText={t("list.error")}
        loadingLabel={t("list.loading")}
        minWidth="min-w-[72rem]"
      />
    </SectionCard>
  )
}

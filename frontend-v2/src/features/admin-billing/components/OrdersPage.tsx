// Every payment order, across every workspace. Read-only (§3-Q5): no refund,
// no re-issue, no payable link — the server never returns `checkout_url` here.
import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { usePaymentProviders } from "@/shared/api/billing"
import { useUrlState } from "@/shared/hooks/useUrlState"
import { DataTable } from "@/shared/ui/DataTable"
import { Pagination } from "@/shared/ui/Pagination"
import { useAdminOrders } from "@/features/admin-billing/api/admin-billing"
import { useDraft } from "@/features/admin-billing/hooks/useDraft"
import { ALL, PAGE_SIZE, parseOffset } from "@/features/admin-billing/lib/filters"
import { OrderFilterBar } from "./OrderFilterBar"
import { useOrderColumns } from "./orderColumns"

const DEFAULTS = { provider: ALL, status: ALL, kind: ALL, from: "", to: "", q: "", offset: "0" }

export function OrdersPage() {
  const { t } = useTranslation("admin-billing")
  const [values, setValues] = useUrlState(DEFAULTS)
  const [draft, setDraft] = useDraft({
    provider: values.provider,
    status: values.status,
    kind: values.kind,
    from: values.from,
    to: values.to,
    q: values.q,
  })
  const offset = parseOffset(values.offset)
  const list = useAdminOrders({
    provider: values.provider,
    status: values.status,
    kind: values.kind,
    from: values.from,
    to: values.to,
    q: values.q,
    offset,
    limit: PAGE_SIZE,
  })
  // The same request the filter bar makes; TanStack dedupes it by key.
  const providers = usePaymentProviders()
  const names = useMemo(
    () => new Map((providers.data?.items ?? []).map((item) => [item.id, item.name])),
    [providers.data],
  )
  const columns = useOrderColumns({
    showWorkspace: true,
    providerName: (id) => names.get(id) ?? id,
  })
  const total = list.data?.total ?? 0

  return (
    <div className="flex flex-col gap-4">
      <OrderFilterBar
        draft={draft}
        onChange={setDraft}
        onApply={() => setValues({ ...draft, q: draft.q.trim() })}
        onReset={() => setValues(DEFAULTS)}
      />
      <section className="rounded-xl border border-hair bg-card p-4">
        <DataTable
          columns={columns}
          rows={list.data?.items ?? []}
          rowKey={(row) => row.id}
          isLoading={list.isPending}
          error={list.error}
          emptyText={t("orders.empty")}
          errorText={t("list.error")}
          loadingLabel={t("list.loading")}
          minWidth="min-w-[84rem]"
        />
        {total > 0 && (
          <Pagination
            offset={offset}
            limit={PAGE_SIZE}
            total={total}
            onOffsetChange={(next) => setValues({ offset: String(next) })}
            labels={{
              previous: t("list.previous"),
              next: t("list.next"),
              nav: t("orders.nav"),
              range: t("list.range", {
                from: offset + 1,
                to: Math.min(offset + PAGE_SIZE, total),
                total,
              }),
            }}
          />
        )}
      </section>
    </div>
  )
}

// One row per workspace: what it is on, until when, and what it has left.
// Read-only by design (§3-Q5) — clicking a row opens the detail view, and
// nothing on this page moves money.
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { useUrlState } from "@/shared/hooks/useUrlState"
import { paths } from "@/shared/router/paths"
import { DataTable } from "@/shared/ui/DataTable"
import { Pagination } from "@/shared/ui/Pagination"
import { useAdminSubscriptions } from "@/features/admin-billing/api/admin-billing"
import { useDraft } from "@/features/admin-billing/hooks/useDraft"
import {
  ALL,
  PAGE_SIZE,
  PLAN_IDS,
  SUBSCRIPTION_STATES,
  parseOffset,
} from "@/features/admin-billing/lib/filters"
import { BillingFilters } from "./BillingFilters"
import { FilterSelect } from "./FilterSelect"
import { SearchField } from "./SearchField"
import { useSubscriptionColumns } from "./subscriptionColumns"

// `offset` has to be one of the defaults, or `useUrlState` will not send a
// changed filter back to page one.
const DEFAULTS = { plan: ALL, state: ALL, q: "", offset: "0" }

export function SubscriptionsPage() {
  const { t } = useTranslation("admin-billing")
  const navigate = useNavigate()
  const [values, setValues] = useUrlState(DEFAULTS)
  // The page cursor is applied immediately; the filters wait for the button.
  const [draft, setDraft] = useDraft({ plan: values.plan, state: values.state, q: values.q })
  const offset = parseOffset(values.offset)
  const list = useAdminSubscriptions({
    plan: values.plan,
    state: values.state,
    q: values.q,
    offset,
    limit: PAGE_SIZE,
  })
  const columns = useSubscriptionColumns()
  const total = list.data?.total ?? 0
  const options = (prefix: string, ids: readonly string[]) => [
    { value: ALL, label: t("common.all") },
    ...ids.map((id) => ({ value: id, label: t(`${prefix}.${id}`) })),
  ]

  return (
    <div className="flex flex-col gap-4">
      <BillingFilters
        label={t("filters.label")}
        onApply={() => setValues({ ...draft, q: draft.q.trim() })}
        onReset={() => setValues(DEFAULTS)}
      >
        <FilterSelect
          label={t("filters.plan")}
          value={draft.plan}
          options={options("plans", PLAN_IDS)}
          onChange={(plan) => setDraft({ ...draft, plan })}
        />
        <FilterSelect
          label={t("filters.state")}
          value={draft.state}
          options={options("states", SUBSCRIPTION_STATES)}
          onChange={(state) => setDraft({ ...draft, state })}
        />
        <SearchField
          label={t("filters.keyword")}
          placeholder={t("subscriptions.searchPlaceholder")}
          value={draft.q}
          onChange={(q) => setDraft({ ...draft, q })}
        />
      </BillingFilters>

      <section className="rounded-xl border border-hair bg-card p-4">
        <DataTable
          columns={columns}
          rows={list.data?.items ?? []}
          rowKey={(row) => row.workspace.id}
          onRowClick={(row) => navigate(paths.adminWorkspace(row.workspace.id))}
          isLoading={list.isPending}
          error={list.error}
          emptyText={t("subscriptions.empty")}
          errorText={t("list.error")}
          loadingLabel={t("list.loading")}
          minWidth="min-w-[64rem]"
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
              nav: t("subscriptions.nav"),
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

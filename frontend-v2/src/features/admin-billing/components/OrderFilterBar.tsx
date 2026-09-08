// The order list's filter row. Split out of the page because six controls plus
// a date-range guard is more than one component should carry.
import { useTranslation } from "react-i18next"
import { usePaymentProviders } from "@/shared/api/billing"
import { ALL, ORDER_KINDS, ORDER_STATUSES } from "@/features/admin-billing/lib/filters"
import type { OrderFilterDraft } from "@/features/admin-billing/types"
import { BillingFilters } from "./BillingFilters"
import { FilterDate } from "./FilterDate"
import { FilterSelect } from "./FilterSelect"
import { SearchField } from "./SearchField"

export function OrderFilterBar({
  draft,
  onChange,
  onApply,
  onReset,
}: {
  draft: OrderFilterDraft
  onChange: (draft: OrderFilterDraft) => void
  onApply: () => void
  onReset: () => void
}) {
  const { t } = useTranslation("admin-billing")
  // There is no admin-side provider registry: channels are deploy-time config,
  // and this is the one endpoint that names them. An empty list only costs the
  // channel filter — every other filter still works.
  const providers = usePaymentProviders()
  // The server answers 422 on a reversed range, so the button refuses first.
  const invalid = Boolean(draft.from && draft.to && draft.from > draft.to)
  const options = (prefix: string, ids: readonly string[]) => [
    { value: ALL, label: t("common.all") },
    ...ids.map((id) => ({ value: id, label: t(`${prefix}.${id}`) })),
  ]

  return (
    <BillingFilters
      label={t("filters.label")}
      invalid={invalid}
      invalidText={t("filters.invalidRange")}
      onApply={onApply}
      onReset={onReset}
    >
      <FilterSelect
        label={t("filters.provider")}
        value={draft.provider}
        options={[
          { value: ALL, label: t("common.all") },
          ...(providers.data?.items ?? []).map((item) => ({ value: item.id, label: item.name })),
        ]}
        onChange={(provider) => onChange({ ...draft, provider })}
      />
      <FilterSelect
        label={t("filters.status")}
        value={draft.status}
        options={options("orderStatus", ORDER_STATUSES)}
        onChange={(status) => onChange({ ...draft, status })}
      />
      <FilterSelect
        label={t("filters.kind")}
        value={draft.kind}
        options={options("kinds", ORDER_KINDS)}
        onChange={(kind) => onChange({ ...draft, kind })}
      />
      <FilterDate
        label={t("filters.from")}
        value={draft.from}
        max={draft.to}
        onChange={(from) => onChange({ ...draft, from })}
      />
      <FilterDate
        label={t("filters.to")}
        value={draft.to}
        min={draft.from}
        onChange={(to) => onChange({ ...draft, to })}
      />
      <SearchField
        label={t("filters.keyword")}
        placeholder={t("orders.searchPlaceholder")}
        value={draft.q}
        onChange={(q) => onChange({ ...draft, q })}
      />
    </BillingFilters>
  )
}

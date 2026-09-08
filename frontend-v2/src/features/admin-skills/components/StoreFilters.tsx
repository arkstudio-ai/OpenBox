import { useTranslation } from "react-i18next"
import { ANY } from "@/features/admin-skills/lib/query"
import { FilterPills, type FilterOption } from "./FilterPills"
import { SearchBox } from "./SearchBox"

const ORIGINS = [ANY, "official", "community", "third_party"] as const
const KINDS = [ANY, "skill", "mcp"] as const
const LISTINGS = [ANY, "listed", "delisted", "pending", "rejected"] as const

export interface StoreFilterValues {
  q: string
  origin: string
  kind: string
  listing: string
}

interface Props {
  values: StoreFilterValues
  onChange: (patch: Partial<StoreFilterValues>) => void
}

/** Search plus the three pill groups above the store table. */
export function StoreFilters({ values, onChange }: Props) {
  const { t } = useTranslation("admin-skills")
  const options = (group: string, values: readonly string[]): FilterOption[] =>
    values.map((value) => ({ value, label: t(`store.${group}.${value}`) }))

  return (
    <div className="flex flex-col gap-2">
      <SearchBox
        value={values.q}
        onChange={(q) => onChange({ q })}
        label={t("store.searchLabel")}
        placeholder={t("store.search")}
      />
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <FilterPills
          label={t("store.filter.origin")}
          options={options("origin", ORIGINS)}
          value={values.origin}
          onChange={(origin) => onChange({ origin })}
        />
        <FilterPills
          label={t("store.filter.kind")}
          options={options("kind", KINDS)}
          value={values.kind}
          onChange={(kind) => onChange({ kind })}
        />
        <FilterPills
          label={t("store.filter.listing")}
          options={options("listing", LISTINGS)}
          value={values.listing}
          onChange={(listing) => onChange({ listing })}
        />
      </div>
    </div>
  )
}

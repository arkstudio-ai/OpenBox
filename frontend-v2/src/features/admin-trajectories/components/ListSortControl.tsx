import { useId } from "react"
import { useTranslation } from "react-i18next"
import { LIST_SORT_LABELS } from "../constants/labels"
import { SESSION_SORTS, type SessionSort } from "../utils/params"

interface ListSortControlProps {
  value: SessionSort
  onChange: (sort: SessionSort) => void
}

/** Chooses the server's order. Changing it restarts paging; rows of a loaded page never move locally. */
export function ListSortControl({ value, onChange }: ListSortControlProps) {
  const { t } = useTranslation("admin-trajectories")
  const id = useId()
  return (
    <span className="text-n600 inline-flex items-center gap-2 text-xs">
      <label htmlFor={id}>{t("list.sort.label")}</label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value as SessionSort)}
        className="border-hair bg-bg text-ink rounded-lg border px-2 py-1 text-xs"
        data-testid="trajectory-list-sort"
      >
        {SESSION_SORTS.map((sort) => (
          <option key={sort} value={sort}>
            {t(LIST_SORT_LABELS[sort])}
          </option>
        ))}
      </select>
    </span>
  )
}

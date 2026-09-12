import { useTranslation } from "react-i18next"
import { ChevronDown } from "lucide-react"

interface FilterMenuProps {
  label: string
  values: readonly string[]
  selected: readonly string[]
  labelFor: (value: string) => string
  onChange: (values: string[]) => void
}

/** A multi-choice filter as a native disclosure: keyboard and screen-reader friendly without extra machinery. */
export function FilterMenu({ label, values, selected, labelFor, onChange }: FilterMenuProps) {
  const { t } = useTranslation("admin-trajectories")
  const toggle = (value: string, checked: boolean) =>
    onChange(checked ? [...selected, value] : selected.filter((item) => item !== value))
  return (
    <details className="relative">
      <summary className="border-hair hover:bg-hairsoft text-n700 flex cursor-pointer list-none items-center gap-1 rounded-full border px-2.5 py-1 text-xs">
        {selected.length ? t("toolbar.filterCount", { label, count: selected.length }) : label}
        <ChevronDown size={12} aria-hidden />
      </summary>
      <div className="border-hair bg-card shadow-pop absolute start-0 z-30 mt-1 flex max-h-72 min-w-44 flex-col gap-0.5 overflow-auto rounded-lg border p-2">
        {!values.length && <span className="text-n500 text-xs">{t("toolbar.noValues")}</span>}
        {values.map((value) => (
          <label
            key={value}
            className="hover:bg-hairsoft text-ink flex items-center gap-2 rounded px-1.5 py-1 text-xs"
          >
            <input
              type="checkbox"
              checked={selected.includes(value)}
              onChange={(event) => toggle(value, event.target.checked)}
            />
            {labelFor(value)}
          </label>
        ))}
        {selected.length > 0 && (
          <button
            type="button"
            className="text-a700 mt-1 w-fit text-xs hover:underline"
            onClick={() => onChange([])}
          >
            {t("toolbar.clearOne")}
          </button>
        )}
      </div>
    </details>
  )
}

import { useState } from "react"
import { useTranslation } from "react-i18next"
import { USAGE_KINDS } from "@/shared/api/billing"
import type { UsageDateFilter } from "@/shared/api/billing"

export function UsageFilters({ onApply }: { onApply: (dates: UsageDateFilter) => void }) {
  const { t } = useTranslation("billing")
  const [from, setFrom] = useState("")
  const [to, setTo] = useState("")
  const [kind, setKind] = useState("")
  const [invalid, setInvalid] = useState(false)
  return (
    <form
      noValidate
      className="border-hair bg-card flex min-w-0 flex-col gap-3 rounded-xl border p-4"
      aria-label={t("usage.dateRange")}
      onSubmit={(event) => {
        event.preventDefault()
        if (from && to && from > to) {
          setInvalid(true)
          return
        }
        setInvalid(false)
        onApply({
          ...(from || to
            ? {
                date_from: from || undefined,
                date_to: to || undefined,
                tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
              }
            : {}),
          ...(kind ? { kind } : {}),
        })
      }}
    >
      <div className="grid min-w-0 grid-cols-1 gap-3 @min-[360px]/billing:grid-cols-3">
        <label className="text-n600 flex min-w-0 flex-col gap-1.5 text-xs">
          {t("usage.kindFilter")}
          <select
            value={kind}
            className="border-hair bg-bg text-ink w-full min-w-0 rounded-lg border px-3 py-2 text-sm"
            onChange={(event) => setKind(event.target.value)}
          >
            <option value="">{t("usage.allKinds")}</option>
            {USAGE_KINDS.map((value) => (
              <option key={value} value={value}>
                {t(`usage.kind.${value}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-n600 flex min-w-0 flex-col gap-1.5 text-xs">
          {t("usage.dateFrom")}
          <input
            type="date"
            value={from}
            max={to || undefined}
            aria-invalid={invalid}
            className="border-hair bg-bg text-ink w-full min-w-0 rounded-lg border px-3 py-2 text-sm"
            onChange={(event) => {
              setFrom(event.target.value)
              setInvalid(false)
            }}
          />
        </label>
        <label className="text-n600 flex min-w-0 flex-col gap-1.5 text-xs">
          {t("usage.dateTo")}
          <input
            type="date"
            value={to}
            min={from || undefined}
            aria-invalid={invalid}
            className="border-hair bg-bg text-ink w-full min-w-0 rounded-lg border px-3 py-2 text-sm"
            onChange={(event) => {
              setTo(event.target.value)
              setInvalid(false)
            }}
          />
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button type="submit" className="bg-ink text-bg rounded-full px-4 py-2 text-xs">
          {t("usage.applyFilter")}
        </button>
        <button
          type="button"
          className="border-hair hover:bg-n200 rounded-full border px-4 py-2 text-xs"
          onClick={() => {
            setFrom("")
            setTo("")
            setKind("")
            setInvalid(false)
            onApply({})
          }}
        >
          {t("usage.resetFilter")}
        </button>
        {invalid && (
          <span role="alert" className="text-n600 text-xs">
            {t("usage.invalidDates")}
          </span>
        )}
      </div>
    </form>
  )
}

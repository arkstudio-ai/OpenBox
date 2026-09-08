// The filter bar both lists sit under: a real <form>, so Enter in the keyword
// box applies the draft instead of reloading the page.
import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"

export function BillingFilters({
  label,
  invalid,
  invalidText,
  onApply,
  onReset,
  children,
}: {
  label: string
  /** Set when the draft cannot be sent yet (e.g. a reversed date range). */
  invalid?: boolean
  invalidText?: string
  onApply: () => void
  onReset: () => void
  children: ReactNode
}) {
  const { t } = useTranslation("admin-billing")
  return (
    <form
      aria-label={label}
      className="flex flex-col gap-3 rounded-xl border border-hair bg-card p-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (!invalid) onApply()
      }}
    >
      <div className="flex flex-wrap items-end gap-3">{children}</div>
      {invalid && invalidText && (
        <p role="alert" className="text-xs text-danger">
          {invalidText}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={invalid}
          className="rounded-full bg-ink px-4 py-1.5 text-xs text-bg disabled:opacity-40"
        >
          {t("list.search")}
        </button>
        <button
          type="button"
          onClick={onReset}
          className="rounded-full border border-hair px-4 py-1.5 text-xs text-n800 hover:bg-hairsoft"
        >
          {t("list.reset")}
        </button>
      </div>
    </form>
  )
}

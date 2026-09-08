// Offset pagination footer for the admin lists. It takes finished strings
// rather than a translation key so `shared/` stays out of the i18n namespaces
// (§10.4: a namespace belongs to a feature, and this belongs to none).

export interface PaginationLabels {
  previous: string
  next: string
  /** Already interpolated by the caller, e.g. "1–20 / 137". */
  range: string
  /** Names the landmark when a page has more than one list. */
  nav?: string
}

export interface PaginationProps {
  offset: number
  limit: number
  total: number
  onOffsetChange: (offset: number) => void
  labels: PaginationLabels
}

const button =
  "rounded-full border border-hair px-3 py-1.5 text-xs text-n800 hover:bg-hairsoft disabled:opacity-50"

export function Pagination({ offset, limit, total, onOffsetChange, labels }: PaginationProps) {
  const hasPrevious = offset > 0
  const hasNext = offset + limit < total

  return (
    <nav
      aria-label={labels.nav}
      className="flex flex-wrap items-center justify-end gap-2 pt-3 text-xs text-n600"
    >
      <span>{labels.range}</span>
      <button
        type="button"
        className={button}
        disabled={!hasPrevious}
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
      >
        {labels.previous}
      </button>
      <button
        type="button"
        className={button}
        disabled={!hasNext}
        onClick={() => onOffsetChange(offset + limit)}
      >
        {labels.next}
      </button>
    </nav>
  )
}

// A calendar-day bound for the order list. `type="date"` gives `YYYY-MM-DD`,
// which is exactly what `/orders` wants — a full ISO datetime answers 422.

export function FilterDate({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string
  value: string
  min?: string
  max?: string
  onChange: (value: string) => void
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1.5 text-xs text-n600">
      {label}
      <input
        type="date"
        value={value}
        min={min || undefined}
        max={max || undefined}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-9 min-w-0 rounded-lg border border-hair bg-bg px-2 text-xs text-ink"
      />
    </label>
  )
}

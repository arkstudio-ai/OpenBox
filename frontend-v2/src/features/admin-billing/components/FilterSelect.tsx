// One labelled dropdown in the filter bar. The options arrive already
// translated: the pages know their own enums, this file only draws them.

export function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: ReadonlyArray<{ value: string; label: string }>
  onChange: (value: string) => void
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1.5 text-xs text-n600">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-9 min-w-0 rounded-lg border border-hair bg-bg px-2 text-xs text-ink"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

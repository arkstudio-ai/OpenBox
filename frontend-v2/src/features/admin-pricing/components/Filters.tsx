// The two filter controls, same look as the billing pages' (the boundaries
// rule keeps features from importing each other, so they live here too).

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

export function SearchField({
  label,
  placeholder,
  value,
  onChange,
}: {
  label: string
  placeholder: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="flex min-w-0 flex-1 basis-64 flex-col gap-1.5 text-xs text-n600">
      {label}
      <input
        type="search"
        maxLength={128}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-9 min-w-0 rounded-lg border border-hair bg-bg px-3 text-xs text-ink"
      />
    </label>
  )
}

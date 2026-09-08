// The keyword box. It is a draft the enclosing form submits rather than a
// live filter: every keystroke would be a server round trip *and* an
// `admin.view_billing` audit row (§4.7).

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

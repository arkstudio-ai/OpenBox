import { cn } from "@/shared/lib/cn"

export interface FilterOption {
  value: string
  label: string
}

interface Props {
  /** Names the group for screen readers; the pills alone do not say what they filter. */
  label: string
  options: readonly FilterOption[]
  value: string
  onChange: (value: string) => void
}

/** One row of mutually exclusive filter pills, mirroring the skill centre's. */
export function FilterPills({ label, options, value, onChange }: Props) {
  return (
    <div role="group" aria-label={label} className="flex flex-none flex-wrap gap-1">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
          className={cn(
            "rounded-full px-2.5 py-1.5 text-xs transition-colors",
            option.value === value ? "bg-hairsoft text-ink" : "text-n600 hover:bg-hairsoft/60",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

// Small form controls the 个人助理 and 语音通话 pages share, in the settings pages' own neutrals.
import { useId, type ReactNode } from "react"
import { cn } from "@/shared/lib/cn"

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: (id: string) => ReactNode
}) {
  const id = useId()
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={id} className="text-ink text-sm font-medium">
        {label}
      </label>
      {children(id)}
      {hint && <span className="text-n600 text-xs text-pretty">{hint}</span>}
    </div>
  )
}

/** One of a few choices, as a row of buttons; the chosen one has the ink border. */
export function Choices<T extends string>({
  label,
  options,
  value,
  text,
  disabled,
  onPick,
}: {
  label: string
  options: readonly T[]
  value: T
  text: (option: T) => string
  disabled?: boolean
  onPick: (option: T) => void
}) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-ink text-sm font-medium">{label}</span>
      <div role="group" aria-label={label} className="flex flex-wrap gap-2">
        {options.map((option) => (
          <button
            key={option}
            type="button"
            aria-pressed={option === value}
            disabled={disabled}
            onClick={() => onPick(option)}
            className={cn(
              "bg-card text-ink h-9 min-w-18 rounded-lg border px-3.5 text-sm transition-colors disabled:opacity-50",
              option === value ? "border-ink" : "border-hair hover:border-n400",
            )}
          >
            {text(option)}
          </button>
        ))}
      </div>
    </div>
  )
}

/** An on/off setting with a line on what it does. */
export function Switch({
  on,
  label,
  hint,
  disabled,
  onToggle,
}: {
  on: boolean
  label: string
  hint?: string
  disabled?: boolean
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={onToggle}
      className={cn(
        "bg-card flex items-center justify-between gap-4 rounded-lg border px-4 py-3.5 text-start disabled:opacity-50",
        on ? "border-ink" : "border-hair",
      )}
    >
      <span className="flex min-w-0 flex-col gap-1">
        <span className="text-base">{label}</span>
        {hint && <span className="text-n600 text-xs text-pretty">{hint}</span>}
      </span>
      <span
        aria-hidden
        className={cn("relative h-5 w-9 flex-none rounded-full transition-colors", on ? "bg-ink" : "bg-n300")}
      >
        <span
          className={cn(
            "bg-bg absolute top-0.5 size-4 rounded-full transition-transform",
            on ? "translate-x-4.5" : "translate-x-0.5",
          )}
        />
      </span>
    </button>
  )
}

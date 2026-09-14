import type { ReactNode } from "react"
import { cn } from "@/shared/lib/cn"

/**
 * Settings vocabulary, taken from DEEIX-Chat's `shared/components/settings-layout`
 * and its account section, spelled in this project's tokens.
 *
 * The reference puts no card around a settings group: rows are bare
 * label-to-control lines held apart by rhythm alone, and a hairline rule
 * divides one group from the next. A border around every group reads heavier
 * than the content deserves once the page owns the whole window. The route
 * owns the section heading, so every settings page — converted or not — keeps
 * the same header and only supplies rows.
 */

/** The rhythm a page's rows sit on. */
export function RowList({ children }: { children: ReactNode }) {
  return <div className="space-y-4 md:space-y-5 xl:space-y-6">{children}</div>
}

/** Hairline between two groups of rows. */
export function SettingsSectionSeparator() {
  return <hr className="border-hair mx-0.5 my-7 border-t md:my-8 xl:mx-1 xl:my-10" />
}

/** Label (with an optional inline hint) on the start, a control on the end. */
export function Row({ label, hint, right }: { label: string; hint?: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex min-w-0 flex-1 items-baseline gap-2">
        <p className="flex-none text-sm font-medium">{label}</p>
        {hint != null && hint !== "" && (
          <p className="text-n600 max-w-[min(60vw,24rem)] truncate text-sm">{hint}</p>
        )}
      </div>
      {right != null && <div className="flex flex-none justify-end">{right}</div>}
    </div>
  )
}

/** Label on the start, a read-only value in a soft slab on the end. */
export function ValueRow({
  label,
  value,
  mono,
  action,
}: {
  label: string
  value: string
  mono?: boolean
  action?: ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <p className="min-w-0 flex-1 text-sm font-medium">{label}</p>
      <div className="bg-hairsoft text-n600 flex max-w-[min(60vw,26rem)] min-w-0 shrink items-center gap-2 rounded-lg px-2 py-1 text-sm">
        <span className={cn("truncate", mono && "font-mono text-xs")}>{value}</span>
        {action}
      </div>
    </div>
  )
}

/** The end-of-row control: a quiet outlined button. */
export function RowButton({
  children,
  onClick,
  disabled,
  tone = "default",
}: {
  children: ReactNode
  onClick?: () => void
  disabled?: boolean
  tone?: "default" | "danger"
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-8 flex-none items-center justify-center rounded-md px-3 text-sm font-medium disabled:opacity-50",
        tone === "danger"
          ? "bg-ink text-bg hover:opacity-90"
          : "border-hair text-ink hover:bg-hairsoft border",
      )}
    >
      {children}
    </button>
  )
}

/** Value control with a caret; opens a menu on click. */
export function ValuePill({ value, onClick }: { value: string; onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="border-hair text-ink hover:bg-hairsoft inline-flex h-8 flex-none items-center gap-1.5 rounded-md border px-3 text-sm font-medium whitespace-nowrap"
    >
      {value}
      <span className="text-n600 text-2xs">▾</span>
    </button>
  )
}

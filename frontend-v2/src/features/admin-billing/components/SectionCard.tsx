// The card every block on the workspace detail page sits in.
import type { ReactNode } from "react"

export function SectionCard({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: ReactNode
}) {
  return (
    <section className="rounded-xl border border-hair bg-card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium text-ink">{title}</h2>
        {hint && <p className="text-2xs text-n500">{hint}</p>}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  )
}

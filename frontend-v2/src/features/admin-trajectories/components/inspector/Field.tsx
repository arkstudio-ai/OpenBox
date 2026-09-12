import type { ReactNode } from "react"

interface FieldListProps {
  children: ReactNode
}

interface FieldProps {
  label: string
  children: ReactNode
}

interface SectionProps {
  title: string
  children: ReactNode
  /** Copy / download controls for the section's value. */
  actions?: ReactNode
}

/** Label/value grid used by every summary-style tab. */
export function FieldList({ children }: FieldListProps) {
  return (
    <dl className="grid grid-cols-[minmax(6.5rem,max-content)_minmax(0,1fr)] gap-x-4 gap-y-2 text-xs">
      {children}
    </dl>
  )
}

export function Field({ label, children }: FieldProps) {
  return (
    <>
      <dt className="text-n500">{label}</dt>
      <dd className="text-ink min-w-0 break-words">{children}</dd>
    </>
  )
}

export function Section({ title, children, actions }: SectionProps) {
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <div className="flex min-h-6 items-center gap-2">
        <h4 className="text-n700 min-w-0 flex-1 truncate text-xs font-medium">{title}</h4>
        {actions}
      </div>
      {children}
    </section>
  )
}

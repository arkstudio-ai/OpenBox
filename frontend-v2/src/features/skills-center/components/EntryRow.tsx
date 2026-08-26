// The row shape every list in the centre uses, so a skill and an MCP server
// read as the same kind of thing — which is what they are to the person
// installing them.
import type { ReactNode } from "react"
import { AlertTriangle } from "lucide-react"
import { EntryIcon } from "./EntryIcon"

export function Badge({
  children,
  tone = "muted",
}: {
  children: ReactNode
  tone?: "muted" | "ok" | "warn"
}) {
  const tones = {
    muted: "bg-n200 text-n700",
    ok: "bg-s100 text-sage",
    warn: "bg-a200 text-n800",
  } as const
  return (
    <span className={`flex-none rounded-md px-1.5 py-0.5 text-[10px] leading-4 ${tones[tone]}`}>
      {children}
    </span>
  )
}

export function IconButton({
  onClick,
  title,
  disabled,
  danger,
  children,
}: {
  onClick: () => void
  title: string
  disabled?: boolean
  danger?: boolean
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={`flex size-7 items-center justify-center rounded-lg text-n600 transition-colors hover:bg-card disabled:opacity-40 ${
        danger ? "hover:text-danger" : "hover:text-ink"
      }`}
    >
      {children}
    </button>
  )
}

export function EntryRow({
  icon,
  name,
  description,
  badges,
  actions,
  warning,
}: {
  icon?: string
  name: string
  description?: string
  badges?: ReactNode
  actions?: ReactNode
  warning?: string
}) {
  return (
    <div className="group flex min-h-16 items-center gap-3 rounded-xl bg-hairsoft/40 px-3 py-2.5 transition-colors hover:bg-hairsoft/70">
      <EntryIcon icon={icon} name={name} />
      <div className="grid min-w-0 flex-1 gap-0.5">
        <div className="flex min-w-0 items-center gap-1.5">
          <h3 className="min-w-0 truncate text-sm font-medium text-ink">{name}</h3>
          {badges}
        </div>
        {description && (
          <p className="min-w-0 truncate text-xs leading-5 text-n600">{description}</p>
        )}
        {warning && (
          <p className="flex items-center gap-1 text-xs leading-5 text-sage">
            <AlertTriangle size={12} aria-hidden />
            {warning}
          </p>
        )}
      </div>
      {actions && <div className="flex flex-none items-center gap-1">{actions}</div>}
    </div>
  )
}

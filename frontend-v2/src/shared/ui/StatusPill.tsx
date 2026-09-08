// The skills centre's Badge look, promoted to shared so a "delisted" pill in
// the admin console and one in the store read as the same state. Tones are
// semantic (§9.2) — callers map a status to a meaning, never to a colour.
import type { ReactNode } from "react"
import { cn } from "@/shared/lib/cn"

export type StatusTone = "ok" | "warn" | "danger" | "muted" | "accent"

const TONES: Record<StatusTone, string> = {
  ok: "bg-s100 text-s800",
  warn: "bg-a200 text-n800",
  // dangerink rather than danger: the softer red is the one that stays legible
  // on dangersoft in dark mode, and it is what the other pills already use.
  danger: "bg-dangersoft text-dangerink",
  muted: "bg-hairsoft text-n700",
  accent: "bg-a100 text-a700",
}

export interface StatusPillProps {
  tone?: StatusTone
  children: ReactNode
  /** The reason behind a one-word status, for anyone who hovers. */
  title?: string
  className?: string
}

export function StatusPill({ tone = "muted", children, title, className }: StatusPillProps) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex flex-none items-center rounded-full px-2 py-0.5 text-2xs leading-4",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

// The collapsed trace row shared by the process / thinking / tool-chain /
// work-log traces: a borderless accordion whose trigger is a two-line block —
// medium 13px title over an 11px muted summary — with the chevron pinned right
// and rotating on open.
import { useState, type ReactNode } from "react"
import { ChevronDown } from "lucide-react"
import { cn } from "@/shared/lib/cn"

interface Props {
  title: string
  subtitle?: string
  /** This trace is currently producing output — shimmers the title. */
  streaming?: boolean
  /** Start open. Only for a finished turn that owes an explanation. */
  defaultOpen?: boolean
  children: ReactNode
}

export function TraceShell({ title, subtitle, streaming, defaultOpen, children }: Props) {
  // Open/closed belongs to the reader, and to nobody else.
  //
  // This used to open itself whenever the trace went live and close itself
  // when the turn started answering. Both flags flip repeatedly within one
  // turn — reasoning alternates with tool calls, and a tool chain goes quiet
  // between two calls — so every flip re-opened a row the reader had just
  // collapsed, and a streaming turn kept several hundred pixels of trace
  // open by default. Now nothing but the toggle moves it: what is on screen
  // stays where it was put.
  //
  // Collapsed is not silent. The title shimmers while the phase runs and the
  // subtitle carries the live count or the call in flight, so the row still
  // says what is happening — in one line instead of a column.
  const [open, setOpen] = useState(Boolean(defaultOpen))

  return (
    <div className="mb-1.5 w-full pe-4 sm:pe-6">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="group flex w-full items-center justify-between gap-2 py-0.5 text-start"
      >
        <span className="flex min-w-0 flex-1 items-baseline gap-2">
          <span
            className={cn(
              "text-md shrink-0 font-medium transition-colors",
              streaming ? "text-shimmer" : "group-hover:text-ink text-n600",
            )}
          >
            {title}
          </span>
          {subtitle ? (
            // One line, beside the title rather than under it: a collapsed
            // trace should cost one row, and four of them stack while a turn
            // runs.
            <span className="text-2xs text-n600/62 min-w-0 flex-1 truncate leading-4 font-normal">
              {subtitle}
            </span>
          ) : null}
        </span>
        <ChevronDown
          className={cn(
            "text-n600 group-hover:text-ink size-3.5 shrink-0 transition-transform duration-200",
            open && "rotate-180",
          )}
        />
      </button>
      <div className="fold" data-open={open}>
        <div>
          <div className="pt-1.5">{children}</div>
        </div>
      </div>
    </div>
  )
}

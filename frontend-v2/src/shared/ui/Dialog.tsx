import { useEffect, useRef, type ReactNode } from "react"
import { createPortal } from "react-dom"
import { keepFocusInside, pushOverlay } from "./overlay-stack"

interface DialogProps {
  open: boolean
  onClose: () => void
  children: ReactNode
  wide?: boolean
  label?: string
}

/** Design-language modal shell: dim scrim + 400px rounded card.
 *  Focus moves in on open (to a `data-autofocus` field when there is one),
 *  stays inside while open, and goes back to whatever opened it on close. */
export function Dialog({ open, onClose, children, wide, label }: DialogProps) {
  const panel = useRef<HTMLDivElement>(null)
  // Parents often pass a fresh onClose on every render; that must not re-run
  // the open effect and pull focus away from a field someone is typing in.
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  }, [onClose])
  useEffect(() => {
    if (!open) return
    const release = pushOverlay()
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const field = panel.current?.querySelector<HTMLElement>("[data-autofocus]")
    ;(field ?? panel.current)?.focus()
    // Editing existing text carries on from its end.
    if (field instanceof HTMLTextAreaElement || (field instanceof HTMLInputElement && field.type === "text"))
      field.setSelectionRange(field.value.length, field.value.length)
    const onKey = (e: KeyboardEvent) => {
      if (!release.isTop()) return
      if (e.key === "Escape") close.current()
      else if (e.key === "Tab" && panel.current) keepFocusInside(e, panel.current)
    }
    window.addEventListener("keydown", onKey)
    return () => {
      window.removeEventListener("keydown", onKey)
      release()
      if (opener?.isConnected) opener.focus()
    }
  }, [open])

  if (!open) return null
  return createPortal(
    <div
      className="bg-n900/30 fixed inset-0 z-50 flex items-center justify-center"
      onClick={onClose}
      role="presentation"
    >
      <div
        ref={panel}
        tabIndex={-1}
        className={`flex ${wide ? "w-180" : "w-100"} border-hair bg-card shadow-pop max-h-[90dvh] max-w-[calc(100vw-2rem)] flex-col gap-2.5 overflow-y-auto rounded-2xl border p-5 outline-none sm:p-6.5`}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>,
    document.body,
  )
}

export function DialogTitle({ children }: { children: ReactNode }) {
  return <span className="text-2xl font-medium tracking-tight">{children}</span>
}

export function DialogBody({ children }: { children: ReactNode }) {
  return <span className="text-n700 text-base leading-relaxed">{children}</span>
}

export function DialogActions({ children }: { children: ReactNode }) {
  return <div className="mt-3 flex items-center justify-end gap-4.5">{children}</div>
}

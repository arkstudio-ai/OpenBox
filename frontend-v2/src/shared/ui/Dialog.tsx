import { useEffect, type ReactNode } from "react"
import { createPortal } from "react-dom"

interface DialogProps {
  open: boolean
  onClose: () => void
  children: ReactNode
  wide?: boolean
  label?: string
}

/** Design-language modal shell: dim scrim + 400px rounded card. */
export function Dialog({ open, onClose, children, wide, label }: DialogProps) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onClose])

  if (!open) return null
  return createPortal(
    <div
      className="bg-n900/30 fixed inset-0 z-50 flex items-center justify-center"
      onClick={onClose}
      role="presentation"
    >
      <div
        className={`flex ${wide ? "w-180" : "w-100"} border-hair bg-card shadow-pop max-h-[90dvh] max-w-[calc(100vw-2rem)] flex-col gap-2.5 overflow-y-auto rounded-2xl border p-5 sm:p-6.5`}
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

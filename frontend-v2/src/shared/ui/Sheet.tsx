import { useEffect, useRef, type ReactNode } from "react"
import { createPortal } from "react-dom"
import { X } from "lucide-react"
import { keepFocusInside, pushOverlay } from "./overlay-stack"

interface SheetProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  /** One line under the title saying what the sheet is for. */
  description?: ReactNode
  /** Accessible name for the close button. */
  closeLabel: string
  children: ReactNode
  /** Pinned under the scrolling body, e.g. a secondary action. */
  footer?: ReactNode
}

/** A panel that slides over the page from the end edge: a list you check and
 *  leave, beside the conversation rather than instead of it. Full width on a
 *  phone. Focus moves in on open, stays inside, and returns on close. */
export function Sheet({ open, onClose, title, description, closeLabel, children, footer }: SheetProps) {
  const panel = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  }, [onClose])
  useEffect(() => {
    if (!open) return
    const release = pushOverlay()
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    panel.current?.focus()
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
    <div className="bg-n900/25 fixed inset-0 z-50 flex justify-end" onClick={onClose} role="presentation">
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        onClick={(e) => e.stopPropagation()}
        className="bg-card border-hair shadow-pop flex h-full w-full max-w-110 flex-col border-s outline-none"
      >
        <div className="flex flex-none items-start gap-3 px-5 pt-5 pb-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-xl font-medium tracking-tight">{title}</h2>
            {description && <p className="text-n600 mt-1 text-sm leading-relaxed">{description}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={closeLabel}
            title={closeLabel}
            className="text-n700 hover:bg-hairsoft flex size-8 flex-none items-center justify-center rounded-full"
          >
            <X size={17} strokeWidth={2.2} />
          </button>
        </div>
        <div className="scr min-h-0 flex-1 overflow-y-auto px-5 pb-5">{children}</div>
        {footer && <div className="border-hair flex-none border-t px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}

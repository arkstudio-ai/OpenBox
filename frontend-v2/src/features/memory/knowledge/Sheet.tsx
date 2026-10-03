import { useEffect, useRef, type ReactNode } from "react"
import { createPortal } from "react-dom"
import { keepFocusInside, pushOverlay } from "@/shared/ui/overlay-stack"

/** A side sheet: the details of one item beside the list it came from.
 *  Full width on a phone, a 28rem column from the inline end elsewhere. */
export function Sheet({
  label,
  onClose,
  children,
}: {
  label: string
  onClose: () => void
  children: ReactNode
}) {
  const panel = useRef<HTMLElement>(null)
  // The page behind refetches in the background; a fresh onClose on every
  // render must not re-run the mount effect and yank focus back to the top.
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  }, [onClose])
  useEffect(() => {
    const release = pushOverlay()
    const previous = document.activeElement as HTMLElement | null
    panel.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      // A dialog opened from the sheet answers its own keys.
      if (!release.isTop()) return
      if (event.key === "Escape") close.current()
      else if (event.key === "Tab" && panel.current) keepFocusInside(event, panel.current)
    }
    window.addEventListener("keydown", onKey)
    return () => {
      window.removeEventListener("keydown", onKey)
      release()
      previous?.focus?.()
    }
  }, [])
  return createPortal(
    <div className="bg-n900/25 fixed inset-0 z-50 flex justify-end" onClick={onClose} role="presentation">
      <aside
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        className="animate-sheet-in border-hair bg-card shadow-float flex h-full w-full flex-col border-s outline-none sm:w-[28rem]"
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </aside>
    </div>,
    document.body,
  )
}

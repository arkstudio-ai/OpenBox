// Drag handle on a column's trailing edge, same mechanic as the workspace
// sidebar's: listeners go on the window so the pointer can outrun the 8px
// strip mid-drag without the column sticking.
//
// The parent must be `relative` — the handle sits on its border, half in and
// half out, so the whole hairline is grabbable rather than just the inside.
import { useRef, type MouseEvent } from "react"

interface Props {
  /** Width at the moment the drag starts; the delta is applied to it. */
  width: number
  onWidth: (width: number) => void
  /** Describes what moves, for screen readers and the tooltip. */
  label: string
}

export function ColumnResizer({ width, onWidth, label }: Props) {
  const origin = useRef<{ x: number; width: number } | null>(null)

  const startDrag = (e: MouseEvent) => {
    e.preventDefault()
    origin.current = { x: e.clientX, width }
    const move = (ev: globalThis.MouseEvent) => {
      if (origin.current) onWidth(origin.current.width + ev.clientX - origin.current.x)
    }
    const up = () => {
      origin.current = null
      window.removeEventListener("mousemove", move)
      window.removeEventListener("mouseup", up)
      // Text across the page selects while dragging otherwise.
      document.body.style.userSelect = ""
    }
    window.addEventListener("mousemove", move)
    window.addEventListener("mouseup", up)
    document.body.style.userSelect = "none"
  }

  return (
    <button
      type="button"
      tabIndex={-1}
      aria-label={label}
      title={label}
      onMouseDown={startDrag}
      // A hover tint is the only thing that says "this is draggable" — the
      // sidebar can rely on people knowing, a column divider cannot.
      className="hover:bg-n400 absolute inset-y-0 -end-1 z-6 w-2 cursor-col-resize bg-transparent transition-colors"
    />
  )
}

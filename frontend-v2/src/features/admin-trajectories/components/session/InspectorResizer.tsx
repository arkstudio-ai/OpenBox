import { useRef, type KeyboardEvent, type PointerEvent } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { INSPECTOR_MAX, INSPECTOR_MIN } from "../../stores/view"

interface InspectorResizerProps {
  width: number
  onResize: (width: number) => void
  className?: string
}

const KEY_STEP = 32

/** Drag or use the arrow keys to widen the detail panel on the end side. */
export function InspectorResizer({ width, onResize, className }: InspectorResizerProps) {
  const { t } = useTranslation("admin-trajectories")
  const drag = useRef<{ x: number; width: number; direction: number } | null>(null)

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId)
    const rtl = getComputedStyle(event.currentTarget).direction === "rtl"
    drag.current = { x: event.clientX, width, direction: rtl ? -1 : 1 }
  }
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current
    if (start) onResize(start.width - (event.clientX - start.x) * start.direction)
  }
  const onPointerUp = () => {
    drag.current = null
  }
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const next: Record<string, number> = {
      ArrowLeft: width + KEY_STEP,
      ArrowRight: width - KEY_STEP,
      Home: INSPECTOR_MAX,
      End: INSPECTOR_MIN,
    }
    if (!(event.key in next)) return
    event.preventDefault()
    onResize(next[event.key])
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t("inspector.resize")}
      aria-valuenow={width}
      aria-valuemin={INSPECTOR_MIN}
      aria-valuemax={INSPECTOR_MAX}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown}
      className={cn(
        "bg-hairsoft hover:bg-a300 focus-visible:bg-a300 w-1.5 flex-none cursor-col-resize touch-none focus-visible:outline-none",
        className,
      )}
      data-testid="trajectory-inspector-resizer"
    />
  )
}

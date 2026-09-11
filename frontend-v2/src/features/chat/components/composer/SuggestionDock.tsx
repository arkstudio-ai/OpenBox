import { useEffect, useRef, type ReactNode, type RefObject } from "react"

/** Pinned beside the input, but scrolling over it still moves the history. */
export function SuggestionDock({ children, historyScrollRef }: {
  children: ReactNode
  historyScrollRef?: RefObject<HTMLDivElement | null>
}) {
  const dockRef = useRef<HTMLDivElement>(null)
  const gesture = useRef({ startY: 0, y: 0, dragging: false })

  useEffect(() => {
    const dock = dockRef.current
    if (!dock || !historyScrollRef) return
    const onWheel = (event: WheelEvent) => {
      const history = historyScrollRef.current
      if (!history || !event.deltaY || event.ctrlKey) return
      event.preventDefault()
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? history.clientHeight : 1
      history.scrollBy({ top: event.deltaY * unit, behavior: "instant" })
    }
    dock.addEventListener("wheel", onWheel, { passive: false })
    return () => dock.removeEventListener("wheel", onWheel)
  }, [historyScrollRef])

  return <div ref={dockRef} className={historyScrollRef ? "touch-pan-x" : undefined}
    onPointerDown={(event) => {
      if (!event.isPrimary) return
      gesture.current = { startY: event.clientY, y: event.clientY, dragging: false }
    }}
    onPointerMove={(event) => {
      const history = historyScrollRef?.current
      if (!history || !event.isPrimary || event.pointerType === "mouse" || !event.buttons) return
      const state = gesture.current
      if (!state.dragging && Math.abs(event.clientY - state.startY) < 6) return
      state.dragging = true
      event.currentTarget.setPointerCapture(event.pointerId)
      history.scrollBy({ top: state.y - event.clientY, behavior: "instant" })
      state.y = event.clientY
    }}
    onPointerCancel={() => { gesture.current.dragging = false }}
    onClickCapture={(event) => {
      if (!gesture.current.dragging || event.detail === 0) return
      event.preventDefault()
      event.stopPropagation()
      gesture.current.dragging = false
    }}>
    {children}
  </div>
}

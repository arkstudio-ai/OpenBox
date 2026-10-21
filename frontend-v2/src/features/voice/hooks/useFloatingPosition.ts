import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react"

interface Point {
  x: number
  y: number
}
interface Size {
  width: number
  height: number
}
interface Drag {
  id: number
  start: Point
  origin: Point
  current: Point
  moved: boolean
}
const MARGIN = 16

function viewport(): Size {
  return { width: window.innerWidth, height: window.innerHeight }
}
function readPosition(key: string): Point | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "null") as Point | null
    return value && Number.isFinite(value.x) && Number.isFinite(value.y) ? value : null
  } catch {
    return null
  }
}
function persist(key: string, point: Point | null) {
  try {
    if (point) localStorage.setItem(key, JSON.stringify(point))
    else localStorage.removeItem(key)
  } catch {
    /* Moving the window must still work when browser storage is unavailable. */
  }
}

/** Account-scoped placement, a drag threshold, keyboard movement, and viewport clamping. */
export function useFloatingPosition(key: string, desired: Size, bottomInset = 0) {
  const [screen, setScreen] = useState(viewport)
  const [saved, setSaved] = useState(() => readPosition(key))
  const drag = useRef<Drag | null>(null)
  const suppressClick = useRef(false)
  const width = Math.min(desired.width, screen.width - 2 * MARGIN)
  const availableHeight = Math.max(1, screen.height - 2 * MARGIN - bottomInset)
  const height = Math.min(desired.height, availableHeight)
  const clamp = (point: Point): Point => ({
    x: Math.max(MARGIN, Math.min(point.x, screen.width - width - MARGIN)),
    y: Math.max(MARGIN, Math.min(point.y, screen.height - height - MARGIN - bottomInset)),
  })
  const point = clamp(saved ?? { x: screen.width - width - 24, y: screen.height - height - 24 - bottomInset })
  useEffect(() => {
    const onResize = () => setScreen(viewport())
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [])
  const move = (next: Point) => {
    setSaved(next)
    persist(key, next)
  }
  return {
    style: { left: point.x, top: point.y, width, height },
    reset: () => {
      setSaved(null)
      persist(key, null)
    },
    handlers: {
      onPointerDown: (event: PointerEvent<HTMLButtonElement>) => {
        if (event.button !== 0) return
        suppressClick.current = false
        drag.current = {
          id: event.pointerId,
          start: { x: event.clientX, y: event.clientY },
          origin: point,
          current: point,
          moved: false,
        }
        event.currentTarget.setPointerCapture(event.pointerId)
      },
      onPointerMove: (event: PointerEvent<HTMLButtonElement>) => {
        const active = drag.current
        if (!active || active.id !== event.pointerId) return
        const dx = event.clientX - active.start.x
        const dy = event.clientY - active.start.y
        if (!active.moved && Math.hypot(dx, dy) < 6) return
        active.moved = true
        active.current = clamp({ x: active.origin.x + dx, y: active.origin.y + dy })
        setSaved(active.current)
      },
      onPointerUp: (event: PointerEvent<HTMLButtonElement>) => {
        const active = drag.current
        if (!active || active.id !== event.pointerId) return
        drag.current = null
        suppressClick.current = active.moved
        if (active.moved) persist(key, active.current)
        event.currentTarget.releasePointerCapture(event.pointerId)
      },
      onLostPointerCapture: () => {
        drag.current = null
      },
      onPointerCancel: () => {
        if (drag.current) setSaved(drag.current.origin)
        drag.current = null
        suppressClick.current = true
      },
      onClickCapture: (event: MouseEvent<HTMLButtonElement>) => {
        if (suppressClick.current && event.detail !== 0) {
          event.preventDefault()
          event.stopPropagation()
        }
        suppressClick.current = false
      },
      onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => {
        const step = event.shiftKey ? 32 : 8
        const directions: Record<string, Point> = {
          ArrowLeft: { x: -step, y: 0 },
          ArrowRight: { x: step, y: 0 },
          ArrowUp: { x: 0, y: -step },
          ArrowDown: { x: 0, y: step },
        }
        const delta = directions[event.key]
        if (!delta) return
        event.preventDefault()
        move(clamp({ x: point.x + delta.x, y: point.y + delta.y }))
      },
    },
  }
}

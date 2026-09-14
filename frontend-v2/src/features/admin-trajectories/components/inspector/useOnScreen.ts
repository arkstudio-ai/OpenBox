import { useEffect, useState } from "react"

/**
 * Whether `element` is actually on screen: mounted, laid out and inside the
 * viewport, not scrolled out of a clipping ancestor such as the inspector
 * panel. Where IntersectionObserver is missing, a mounted element counts as
 * shown.
 */
export function useOnScreen(element: Element | null): boolean {
  const [intersecting, setIntersecting] = useState(true)
  useEffect(() => {
    if (!element || typeof IntersectionObserver === "undefined") return
    const observer = new IntersectionObserver((entries) => {
      const latest = entries[entries.length - 1]
      if (latest) setIntersecting(latest.isIntersecting)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [element])
  return element !== null && intersecting
}

/**
 * Which transient overlays — modal dialogs, floating menus, drawers — are open
 * right now.
 *
 * Every one of them closes on Escape from its own `window` keydown listener,
 * and none of them stops the event, so anything else that wants Escape has to
 * decide for itself whether the key was already spoken for. Listener order
 * cannot decide it: the takeover topbar mounts with the workspace shell, long
 * before any overlay opens, so its listener always runs first and would see a
 * pristine, un-prevented event. Overlays therefore announce themselves here and
 * the topbar yields while the count is above zero.
 */
const stack: object[] = []

export interface OverlayRelease {
  /** Call exactly once, on close. */
  (): void
  /** Whether this overlay is the one in front. A dialog opened over a drawer
   *  answers Escape alone; the drawer behind it stays open. */
  isTop: () => boolean
}

/** Register an open overlay. Call the returned release exactly once, on close. */
export function pushOverlay(): OverlayRelease {
  const token = {}
  stack.push(token)
  let released = false
  const release = (() => {
    if (released) return
    released = true
    stack.splice(stack.indexOf(token), 1)
  }) as OverlayRelease
  release.isTop = () => stack.at(-1) === token
  return release
}

/** Whether Escape belongs to an overlay rather than to the page behind it. */
export function overlayOpen(): boolean {
  return stack.length > 0
}

const FOCUSABLE =
  "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"

/** Tab and Shift+Tab go round a modal's own controls, never the page behind it. */
export function keepFocusInside(event: KeyboardEvent, panel: HTMLElement) {
  const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)]
  const first = items[0] ?? panel
  const last = items.at(-1) ?? panel
  const active = document.activeElement
  const leaving = event.shiftKey ? active === first || active === panel : active === last
  if (panel.contains(active) && !leaving) return
  event.preventDefault()
  ;(event.shiftKey ? last : first).focus()
}

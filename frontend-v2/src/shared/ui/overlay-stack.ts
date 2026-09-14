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
let openCount = 0

/** Register an open overlay. Call the returned release exactly once, on close. */
export function pushOverlay(): () => void {
  openCount += 1
  let released = false
  return () => {
    if (released) return
    released = true
    openCount -= 1
  }
}

/** Whether Escape belongs to an overlay rather than to the page behind it. */
export function overlayOpen(): boolean {
  return openCount > 0
}

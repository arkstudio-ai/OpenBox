export const DESKTOP_NOT_READY_EVENT = "openbox:desktop-not-ready"

export function requestDesktopPanel(): void {
  window.dispatchEvent(new Event(DESKTOP_NOT_READY_EVENT))
}

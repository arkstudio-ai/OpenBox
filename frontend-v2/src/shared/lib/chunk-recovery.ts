// A tab opened before a deployment can still reference the previous hashed
// chunks. Refresh the document once, not an API call or a submitted task.
const RELOAD_KEY = "openbox:chunk-reload-at"
const RELOAD_COOLDOWN_MS = 5 * 60_000

export function isChunkLoadError(error: unknown): boolean {
  const message = error && typeof error === "object" && "message" in error ? error.message : error
  return (
    typeof message === "string" &&
    /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Loading (?:CSS )?chunk [\w-]+ failed|Unable to preload CSS for/i.test(
      message,
    )
  )
}

export function reloadPage(): void {
  window.location.reload()
}

/**
 * Reload the document at most once per cooldown window for this tab.
 * Shared by every automatic recovery path so two triggers (a missing chunk
 * and a stale build, say) cannot chase each other into a loop.
 */
export function reloadOnce(): boolean {
  try {
    const previous = Number(window.sessionStorage.getItem(RELOAD_KEY))
    const now = Date.now()
    // Do not clear this on startup: a broken new build must remain recoverable
    // via the manual button rather than refreshing forever.
    if (previous > 0 && now - previous < RELOAD_COOLDOWN_MS) return false
    window.sessionStorage.setItem(RELOAD_KEY, String(now))
    reloadPage()
    return true
  } catch {
    // Without durable per-tab storage we cannot safely bound automatic reloads.
    return false
  }
}

export function recoverChunkLoadError(error: unknown): boolean {
  if (!isChunkLoadError(error) || !navigator.onLine) return false
  return reloadOnce()
}

/**
 * Catch resource failures that never reach a React error boundary: Vite's
 * preload helper (`vite:preloadError`) and `import()` calls made from event
 * handlers or stores, which surface as unhandled rejections.
 */
export function installChunkRecovery(target: Window = window): () => void {
  const onPreloadError = (event: Event) => {
    const payload = (event as Event & { payload?: unknown }).payload
    if (recoverChunkLoadError(payload)) event.preventDefault()
  }
  const onRejection = (event: PromiseRejectionEvent) => {
    if (recoverChunkLoadError(event.reason)) event.preventDefault()
  }
  target.addEventListener("vite:preloadError", onPreloadError)
  target.addEventListener("unhandledrejection", onRejection)
  return () => {
    target.removeEventListener("vite:preloadError", onPreloadError)
    target.removeEventListener("unhandledrejection", onRejection)
  }
}

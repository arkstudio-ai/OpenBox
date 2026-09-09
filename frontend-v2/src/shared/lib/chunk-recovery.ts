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

export function recoverChunkLoadError(error: unknown): boolean {
  if (!isChunkLoadError(error) || !navigator.onLine) return false
  try {
    const previous = Number(window.sessionStorage.getItem(RELOAD_KEY))
    const now = Date.now()
    // Shared across chunk URLs so a second missing dependency cannot loop.
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

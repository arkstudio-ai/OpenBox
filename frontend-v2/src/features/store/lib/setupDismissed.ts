// The per-workspace "稍后" on the first-run store form. localStorage may be
// unavailable (private browsing, blocked site data); then the skip lasts
// the session and the form comes back next time, which is the safe side.
export function storeSetupDismissedKey(workspaceId: string): string {
  return `bossip:store-setup-dismissed:${workspaceId}`
}

export function readStoreSetupDismissed(workspaceId: string): boolean {
  try {
    return localStorage.getItem(storeSetupDismissedKey(workspaceId)) === "1"
  } catch {
    return false
  }
}

export function writeStoreSetupDismissed(workspaceId: string): void {
  try {
    localStorage.setItem(storeSetupDismissedKey(workspaceId), "1")
  } catch {
    /* Private browsing: the skip lasts the session. */
  }
}

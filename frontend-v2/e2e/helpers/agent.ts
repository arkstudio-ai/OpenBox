import type { Page } from "@playwright/test"

/**
 * Wait until no agent run is in flight for this account.
 *
 * The backend allows one concurrent agent per user (max_concurrent_agents),
 * so a test that sends a prompt while a previous test's run is still winding
 * down gets a 429 and silently does nothing — which then shows up as a
 * baffling "the stop button never appeared". Serialise on the real signal.
 */
export async function waitForIdleAgent(page: Page, timeoutMs = 120_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const busy = await page.evaluate(async () => {
      const state = window as unknown as { __openboxE2EToken?: string }
      const refresh = async (): Promise<string | undefined> => {
        const response = await fetch("/api/auth/refresh", {
          method: "POST",
          credentials: "include",
        })
        if (!response.ok) return undefined
        const body = (await response.json()) as { access_token?: string }
        state.__openboxE2EToken = body.access_token
        return body.access_token
      }
      let token = state.__openboxE2EToken ?? (await refresh())
      if (!token) return -1
      let res = await fetch("/api/agent/session", {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (res.status === 401) {
        token = await refresh()
        if (!token) return -1
        res = await fetch("/api/agent/session", {
          headers: { Authorization: `Bearer ${token}` },
        })
      }
      if (!res.ok) return -1
      const sessions = (await res.json()) as { status?: string }[]
      return sessions.filter((s) => ["busy", "retry", "finalizing", "compacting"].includes(s.status ?? "")).length
    })
    if (busy === 0) return
    await page.waitForTimeout(2000)
  }
  throw new Error("agent still busy after waiting; the concurrency slot never freed")
}

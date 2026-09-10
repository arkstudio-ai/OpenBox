// Every build carries one id, in the bundle (`__APP_BUILD__`) and in
// index.html (`<meta name="app-build">`, served no-store). A running tab that
// sees a different id on the server knows a deployment happened behind it and
// swaps itself for the new build at a quiet moment, instead of hitting a
// missing chunk or a changed contract later and showing an error page.
import { reloadOnce } from "@/shared/lib/chunk-recovery"

export const BUILD_META_NAME = "app-build"
const META_RE = /<meta\s+name="app-build"\s+content="([^"]*)"/i

export function currentBuild(): string {
  return typeof __APP_BUILD__ === "string" && __APP_BUILD__ ? __APP_BUILD__ : "dev"
}

/** The id stamped into the index.html the server hands out right now; null when unknown. */
export async function fetchServerBuild(fetcher: typeof fetch = fetch): Promise<string | null> {
  try {
    const res = await fetcher("/index.html", { cache: "no-store", headers: { accept: "text/html" } })
    if (!res.ok) return null
    const match = META_RE.exec(await res.text())
    return match?.[1] ?? null
  } catch {
    return null
  }
}

export async function isBuildStale(fetcher?: typeof fetch): Promise<boolean> {
  const current = currentBuild()
  if (current === "dev") return false
  const server = await fetchServerBuild(fetcher)
  return server !== null && server !== current
}

/**
 * Last resort for an error page: if the server is already on a newer build,
 * the error is almost certainly staleness — reload once instead of asking
 * the person to.
 */
export async function reloadIfStale(fetcher?: typeof fetch): Promise<boolean> {
  if (!navigator.onLine) return false
  return (await isBuildStale(fetcher)) && reloadOnce()
}

export interface BuildWatchOptions {
  /** True while something in the tab must not be interrupted (a streaming turn). */
  isBusy?: () => boolean
  /** How often to look while the tab stays open. */
  intervalMs?: number
  /** Minimum gap between two checks, whatever triggers them. */
  minGapMs?: number
  fetcher?: typeof fetch
  target?: Window
  doc?: Document
}

export interface BuildWatch {
  stop: () => void
  /** True once a newer build has been seen on the server. */
  isStale: () => boolean
  /** Check now (subject to the min gap); resolves to the stale flag. */
  check: () => Promise<boolean>
  /**
   * Called by the router on a client-side navigation: a page change is the
   * quietest moment to swap builds, so a stale tab reloads at the new URL.
   */
  onNavigate: () => void
}

/**
 * Notice a deployment while the tab is open and reload at a quiet moment:
 * right away while the tab is hidden, or at the next in-app navigation while
 * it is visible. Never while a turn is streaming.
 */
export function watchBuild(options: BuildWatchOptions = {}): BuildWatch {
  const {
    isBusy = () => false,
    intervalMs = 10 * 60_000,
    minGapMs = 60_000,
    fetcher,
    target = window,
    doc = document,
  } = options
  let stale = false
  let lastCheck = 0
  let inFlight: Promise<boolean> | null = null

  const hidden = () => doc.visibilityState === "hidden"

  const swap = () => {
    if (!stale || isBusy()) return false
    return reloadOnce()
  }

  const check = async (): Promise<boolean> => {
    if (stale) return true
    if (inFlight) return inFlight
    const now = Date.now()
    if (now - lastCheck < minGapMs) return false
    lastCheck = now
    inFlight = isBuildStale(fetcher)
      .then((result) => {
        stale = result
        if (stale && hidden()) swap()
        return stale
      })
      .finally(() => {
        inFlight = null
      })
    return inFlight
  }

  const onVisibility = () => {
    if (hidden()) {
      if (stale) swap()
      else void check()
    } else {
      void check()
    }
  }
  const onFocus = () => void check()
  const onOnline = () => void check()

  doc.addEventListener("visibilitychange", onVisibility)
  target.addEventListener("focus", onFocus)
  target.addEventListener("online", onOnline)
  const timer = target.setInterval(() => void check(), intervalMs)

  return {
    stop: () => {
      doc.removeEventListener("visibilitychange", onVisibility)
      target.removeEventListener("focus", onFocus)
      target.removeEventListener("online", onOnline)
      target.clearInterval(timer)
    },
    isStale: () => stale,
    check,
    onNavigate: () => {
      swap()
    },
  }
}

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { fetchServerBuild, isBuildStale, reloadIfStale, watchBuild } from "./build-version"

function html(build: string) {
  return `<!doctype html><html><head>\n    <meta name="app-build" content="${build}" />\n<meta charset="UTF-8" /></head><body></body></html>`
}

function fetcherServing(build: string | null, ok = true) {
  return vi.fn(async () => ({ ok, text: async () => (build === null ? "<html></html>" : html(build)) })) as unknown as typeof fetch
}

interface FakeDoc {
  visibilityState: DocumentVisibilityState
  listeners: Map<string, () => void>
  addEventListener: (name: string, fn: () => void) => void
  removeEventListener: (name: string) => void
  fire: (name: string) => void
}

function fakeDoc(visibility: DocumentVisibilityState = "visible"): FakeDoc {
  const listeners = new Map<string, () => void>()
  return {
    visibilityState: visibility,
    listeners,
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name) => listeners.delete(name),
    fire: (name) => listeners.get(name)?.(),
  }
}

let reload: ReturnType<typeof vi.fn>
let store: Map<string, string>

beforeEach(() => {
  store = new Map()
  reload = vi.fn()
  vi.stubGlobal("__APP_BUILD__", "build-1")
  vi.stubGlobal("navigator", { onLine: true })
  vi.stubGlobal("window", {
    location: { reload },
    sessionStorage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => store.set(k, v) },
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    setInterval: vi.fn(() => 1),
    clearInterval: vi.fn(),
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("build id on the server", () => {
  it("reads the meta stamped into index.html and tolerates a page without it", async () => {
    expect(await fetchServerBuild(fetcherServing("build-2"))).toBe("build-2")
    expect(await fetchServerBuild(fetcherServing(null))).toBeNull()
    expect(await fetchServerBuild(fetcherServing("build-2", false))).toBeNull()
  })

  it("is stale only when the server carries a different id than the running bundle", async () => {
    expect(await isBuildStale(fetcherServing("build-1"))).toBe(false)
    expect(await isBuildStale(fetcherServing("build-2"))).toBe(true)
    expect(await isBuildStale(fetcherServing(null))).toBe(false)
    vi.stubGlobal("__APP_BUILD__", "dev")
    expect(await isBuildStale(fetcherServing("build-2"))).toBe(false)
  })
})

describe("error page on a stale tab", () => {
  it("reloads once when the server already has a newer build", async () => {
    expect(await reloadIfStale(fetcherServing("build-2"))).toBe(true)
    expect(reload).toHaveBeenCalledTimes(1)
    // the shared cooldown stops a second automatic reload
    expect(await reloadIfStale(fetcherServing("build-2"))).toBe(false)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it("leaves a genuine error alone when the build is current or offline", async () => {
    expect(await reloadIfStale(fetcherServing("build-1"))).toBe(false)
    vi.stubGlobal("navigator", { onLine: false })
    expect(await reloadIfStale(fetcherServing("build-2"))).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })
})

describe("watching for a deployment behind an open tab", () => {
  it("swaps a hidden tab as soon as it notices the new build", async () => {
    const doc = fakeDoc("hidden")
    const watch = watchBuild({ fetcher: fetcherServing("build-2"), doc: doc as unknown as Document, minGapMs: 0 })
    expect(await watch.check()).toBe(true)
    expect(reload).toHaveBeenCalledTimes(1)
    watch.stop()
  })

  it("waits for the next in-app navigation while the tab is visible", async () => {
    const doc = fakeDoc("visible")
    const watch = watchBuild({ fetcher: fetcherServing("build-2"), doc: doc as unknown as Document, minGapMs: 0 })
    doc.fire("visibilitychange")
    await vi.waitFor(() => expect(watch.isStale()).toBe(true))
    expect(reload).not.toHaveBeenCalled()
    watch.onNavigate()
    expect(reload).toHaveBeenCalledTimes(1)
    watch.stop()
  })

  it("never interrupts a busy tab, and swaps once it goes hidden", async () => {
    const doc = fakeDoc("visible")
    let busy = true
    const watch = watchBuild({ fetcher: fetcherServing("build-2"), doc: doc as unknown as Document, minGapMs: 0, isBusy: () => busy })
    expect(await watch.check()).toBe(true)
    watch.onNavigate()
    expect(reload).not.toHaveBeenCalled()
    busy = false
    doc.visibilityState = "hidden"
    doc.fire("visibilitychange")
    expect(reload).toHaveBeenCalledTimes(1)
    watch.stop()
  })

  it("does not hammer the server: one request per min gap, none once stale", async () => {
    const fetcher = fetcherServing("build-1")
    const doc = fakeDoc("visible")
    const watch = watchBuild({ fetcher, doc: doc as unknown as Document, minGapMs: 60_000 })
    await watch.check()
    await watch.check()
    doc.fire("visibilitychange")
    expect(fetcher).toHaveBeenCalledTimes(1)
    watch.stop()
  })
})

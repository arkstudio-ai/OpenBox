import { afterEach, describe, expect, it, vi } from "vitest"
import { isChunkLoadError, recoverChunkLoadError } from "./chunk-recovery"

const chunkError = new TypeError(
  "Failed to fetch dynamically imported module: https://app.test/assets/old.js",
)

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function browser() {
  const store = new Map<string, string>()
  const reload = vi.fn()
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => store.set(k, v),
  }
  vi.stubGlobal("window", { location: { reload }, sessionStorage: storage })
  vi.stubGlobal("navigator", { onLine: true })
  return { reload, storage }
}

describe("chunk recovery", () => {
  it.each([
    chunkError,
    new TypeError("error loading dynamically imported module"),
    new TypeError("Importing a module script failed."),
    new Error("Unable to preload CSS for /assets/old.css"),
    new Error("Loading chunk 42 failed"),
  ])("recognizes browser/bundler resource failures: %s", (error) =>
    expect(isChunkLoadError(error)).toBe(true),
  )

  it.each([
    null,
    undefined,
    { status: 502 },
    new Error("HTTP 502"),
    new TypeError("Failed to fetch"),
    new TypeError("Cannot read properties of undefined"),
  ])("does not reload for an API/network/programming error: %s", (error) => {
    const { reload } = browser()
    expect(isChunkLoadError(error)).toBe(false)
    expect(recoverChunkLoadError(error)).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })

  it("reloads only once across repeated errors and different missing chunks", () => {
    const { reload } = browser()
    expect(recoverChunkLoadError(chunkError)).toBe(true)
    expect(recoverChunkLoadError(chunkError)).toBe(false)
    expect(recoverChunkLoadError(new Error("Unable to preload CSS for /assets/other.css"))).toBe(false)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it("allows recovery again after the cooldown, not immediately after startup", () => {
    const { reload } = browser()
    const now = Date.now()
    vi.spyOn(Date, "now").mockReturnValue(now)
    recoverChunkLoadError(chunkError)
    vi.spyOn(Date, "now").mockReturnValue(now + 5 * 60_000)
    expect(recoverChunkLoadError(chunkError)).toBe(true)
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it("does not automatically reload offline", () => {
    const { reload } = browser()
    vi.stubGlobal("navigator", { onLine: false })
    expect(recoverChunkLoadError(chunkError)).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })

  it.each(["getItem", "setItem"] as const)(
    "falls back to manual recovery if storage %s is blocked",
    (method) => {
      const { reload, storage } = browser()
      vi.spyOn(storage, method).mockImplementation(() => {
        throw new Error("Storage blocked")
      })
      expect(recoverChunkLoadError(chunkError)).toBe(false)
      expect(reload).not.toHaveBeenCalled()
    },
  )
})

import { createElement, type ReactNode } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { MemoryRouter, useLocation } from "react-router"
import { useUrlState, type UrlStateOptions } from "./useUrlState"

const DEFAULTS = { q: "", origin: "all", offset: "0" } as const

function setup(
  entry = "/app/admin/skills",
  defaults: Record<string, string> = { ...DEFAULTS },
  options: UrlStateOptions<string> = {},
) {
  const seen = { search: "" }
  function Recorder() {
    seen.search = useLocation().search
    return null
  }
  const view = renderHook(() => useUrlState(defaults, options), {
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(MemoryRouter, { initialEntries: [entry] }, createElement(Recorder), children),
  })
  return { ...view, seen }
}

afterEach(cleanup)

describe("useUrlState", () => {
  it("falls back to the defaults and reads what the URL carries", () => {
    const { result } = setup("/app/admin/skills?q=git&offset=40")
    expect(result.current[0]).toEqual({ q: "git", origin: "all", offset: "40" })
  })

  it("merges a patch and keeps defaults out of the URL", () => {
    const { result, seen } = setup()
    act(() => result.current[1]({ q: "git" }))
    expect(result.current[0]).toEqual({ q: "git", origin: "all", offset: "0" })
    expect(seen.search).toBe("?q=git")

    act(() => result.current[1]({ origin: "community" }))
    expect(seen.search).toBe("?q=git&origin=community")

    // Back to the default: the key leaves the address rather than being pinned.
    act(() => result.current[1]({ origin: "all" }))
    expect(seen.search).toBe("?q=git")
    act(() => result.current[1]({ q: undefined }))
    expect(seen.search).toBe("")
    expect(result.current[0]).toEqual(DEFAULTS)
  })

  it("sends a filter change back to the first page but lets paging stand", () => {
    const { result, seen } = setup("/app/admin/skills?offset=40")
    act(() => result.current[1]({ offset: "80" }))
    expect(seen.search).toBe("?offset=80")

    act(() => result.current[1]({ q: "git" }))
    expect(seen.search).toBe("?q=git")
    expect(result.current[0].offset).toBe("0")

    // Re-applying the same filter is not a change, so the page stays put.
    act(() => result.current[1]({ offset: "40" }))
    act(() => result.current[1]({ q: "git" }))
    expect(result.current[0].offset).toBe("40")
  })

  it("leaves query keys it was not given alone", () => {
    const { result, seen } = setup("/app/admin/skills?tab=store&q=git")
    act(() => result.current[1]({ q: "mcp" }))
    expect(seen.search).toBe("?tab=store&q=mcp")
  })

  // A key that addresses a row inside the list — which submission is open —
  // narrows nothing, so paging to row 41 and opening it must not snap the list
  // back to row 1 with the opened row scrolled off it.
  it("leaves the cursor alone for keys that only address something in the list", () => {
    const { result, seen } = setup(
      "/app/admin/skills?offset=40",
      { id: "", state: "pending", offset: "0" },
      {
        keepPage: ["id"],
      },
    )
    act(() => result.current[1]({ id: "community:42" }))
    expect(seen.search).toBe("?offset=40&id=community%3A42")
    expect(result.current[0].offset).toBe("40")

    // A real filter still resets it, pinned key present or not.
    act(() => result.current[1]({ state: "rejected" }))
    expect(result.current[0].offset).toBe("0")
  })

  it("does not invent an offset for a list that has none", () => {
    const { result, seen } = setup("/app/admin/skills?offset=40", { q: "" })
    act(() => result.current[1]({ q: "git" }))
    expect(seen.search).toBe("?offset=40&q=git")
  })
})

import { renderHook } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { useStableWatermark } from "./useStableWatermark"

describe("useStableWatermark", () => {
  it("follows the head, then holds while the reader is paging", () => {
    const { result, rerender } = renderHook(({ live, follow }) => useStableWatermark(live, follow), {
      initialProps: { live: "10" as string | null, follow: true },
    })
    expect(result.current).toBe("10")
    rerender({ live: "12", follow: true })
    expect(result.current).toBe("12")
    rerender({ live: "12", follow: false })
    rerender({ live: "40", follow: false })
    expect(result.current).toBe("12")
    rerender({ live: "40", follow: true })
    expect(result.current).toBe("40")
  })

  it("pins the first known head when paging starts before it arrived", () => {
    const { result, rerender } = renderHook(({ live, follow }) => useStableWatermark(live, follow), {
      initialProps: { live: null as string | null, follow: false },
    })
    expect(result.current).toBeNull()
    rerender({ live: "7", follow: false })
    expect(result.current).toBe("7")
    rerender({ live: "9", follow: false })
    expect(result.current).toBe("7")
  })
})

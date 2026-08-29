import { act, renderHook } from "@testing-library/react"
import { beforeEach, describe, expect, it } from "vitest"
import { useVideoModelChoice } from "./useVideoModelChoice"
import { useVideoModelChoiceStore } from "../stores/video-model-choice"

beforeEach(() => {
  useVideoModelChoiceStore.setState({ picked: new Map() })
})

describe("useVideoModelChoice", () => {
  it("prefers an unsent pick over the session's recorded model", () => {
    const { result } = renderHook(() =>
      useVideoModelChoice({ sessionVideoModel: "wan3.0-video", sessionKey: "s1", fallback: "d" }),
    )
    expect(result.current.activeId).toBe("wan3.0-video")
    act(() => result.current.pick("wan3.0-video-prime"))
    expect(result.current.activeId).toBe("wan3.0-video-prime")
  })

  it("falls back through session then deployment default", () => {
    const { result } = renderHook(() =>
      useVideoModelChoice({ sessionKey: "s2", fallback: "deployment-default" }),
    )
    expect(result.current.activeId).toBe("deployment-default")
  })

  it("treats an empty session model as no choice", () => {
    // The backend serialises "no pick" as "", not null; without this the
    // picker would render a blank label instead of the default.
    const { result } = renderHook(() =>
      useVideoModelChoice({ sessionVideoModel: "", sessionKey: "s3", fallback: "deployment-default" }),
    )
    expect(result.current.activeId).toBe("deployment-default")
  })

  it("only reports an actual pick as pending", () => {
    // What gets sent to the backend. Sending the resolved default would pin
    // every conversation to it, including ones that never chose.
    const { result } = renderHook(() =>
      useVideoModelChoice({ sessionVideoModel: "wan3.0-video", sessionKey: "s4", fallback: "d" }),
    )
    expect(result.current.pending).toBeUndefined()
    act(() => result.current.pick("wan3.0-video-prime"))
    expect(result.current.pending).toBe("wan3.0-video-prime")
  })

  it("drops an unsent pick when the conversation is left", () => {
    const { result, unmount } = renderHook(() => useVideoModelChoice({ sessionKey: "s5" }))
    act(() => result.current.pick("wan3.0-video"))
    expect(useVideoModelChoiceStore.getState().picked.get("s5")).toBe("wan3.0-video")
    unmount()
    expect(useVideoModelChoiceStore.getState().picked.has("s5")).toBe(false)
  })

  it("keeps picks in separate conversations apart", () => {
    const a = renderHook(() => useVideoModelChoice({ sessionKey: "a" }))
    const b = renderHook(() => useVideoModelChoice({ sessionKey: "b", fallback: "d" }))
    act(() => a.result.current.pick("wan3.0-video"))
    expect(b.result.current.activeId).toBe("d")
  })
})

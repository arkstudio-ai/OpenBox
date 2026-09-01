import { act, renderHook } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import type { ModelInfo } from "@/shared/types/api"
import { useReasoningChoice } from "./useReasoningChoice"

const GPT: ModelInfo = {
  id: "openai/gpt-5.4",
  name: "GPT-5.4",
  variants: ["none", "low", "medium", "high", "xhigh"],
  default_variant: "none",
}
const DEEPSEEK: ModelInfo = {
  id: "deepseek/deepseek-v4-flash",
  name: "DeepSeek V4 Flash",
  variants: ["off", "low", "high", "max"],
  default_variant: "high",
}

describe("useReasoningChoice", () => {
  it("restores a persisted strength only for the model that owns it", () => {
    const { result, rerender } = renderHook(
      ({ model }) =>
        useReasoningChoice({
          model,
          sessionModel: GPT.id,
          sessionVariant: "xhigh",
          sessionKey: "s1",
        }),
      { initialProps: { model: GPT } },
    )

    expect(result.current.activeId).toBe("xhigh")
    expect(result.current.value).toBeUndefined()
    rerender({ model: DEEPSEEK })
    expect(result.current.activeId).toBeNull()
    expect(result.current.value).toBeNull()
  })

  it("remembers independent picks while switching models", () => {
    const { result, rerender } = renderHook(({ model }) => useReasoningChoice({ model, sessionKey: "s1" }), {
      initialProps: { model: GPT },
    })

    act(() => result.current.pick("high"))
    expect(result.current.activeId).toBe("high")
    expect(result.current.value).toBe("high")

    rerender({ model: DEEPSEEK })
    act(() => result.current.pick("max"))
    expect(result.current.activeId).toBe("max")
    expect(result.current.value).toBe("max")

    rerender({ model: GPT })
    expect(result.current.activeId).toBe("high")
  })

  it("uses null as an explicit return to the model default", () => {
    const { result } = renderHook(() =>
      useReasoningChoice({
        model: GPT,
        sessionModel: GPT.id,
        sessionVariant: "high",
        sessionKey: "s1",
      }),
    )

    act(() => result.current.pick(null))
    expect(result.current.activeId).toBeNull()
    expect(result.current.defaultId).toBe("none")
    expect(result.current.value).toBeNull()
  })

  it("does not offer a control for a model without declared variants", () => {
    const { result } = renderHook(() =>
      useReasoningChoice({
        model: { id: "plain", name: "Plain" },
        sessionKey: "s1",
      }),
    )

    expect(result.current.variants).toEqual([])
    expect(result.current.activeId).toBeUndefined()
    expect(result.current.value).toBeUndefined()
  })

  it("clears a persisted effort when switching to a model with no selector", () => {
    const { result } = renderHook(() =>
      useReasoningChoice({
        model: { id: "plain", name: "Plain" },
        sessionModel: GPT.id,
        sessionVariant: "high",
        sessionKey: "s1",
      }),
    )

    expect(result.current.activeId).toBeUndefined()
    expect(result.current.value).toBeNull()
  })

  it("normalizes a stale strength the current model no longer advertises", () => {
    const { result } = renderHook(() =>
      useReasoningChoice({
        model: DEEPSEEK,
        sessionModel: DEEPSEEK.id,
        sessionVariant: "medium",
        sessionKey: "s1",
      }),
    )

    expect(result.current.activeId).toBeNull()
    expect(result.current.value).toBeNull()
  })
})

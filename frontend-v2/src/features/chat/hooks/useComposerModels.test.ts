import { act, renderHook } from "@testing-library/react"
import { beforeEach, describe, expect, it } from "vitest"
import type { AppConfig } from "@/shared/types/api"
import { useComposerModels } from "./useComposerModels"
import { useModelChoiceStore } from "../stores/model-choice"
import { useVideoModelChoiceStore } from "../stores/video-model-choice"

const CONFIG: AppConfig = {
  models: [
    {
      id: "openai/gemini-3.8-flash",
      name: "Gemini 3.8 Flash",
      variants: ["low", "medium", "high"],
      default_variant: "medium",
    },
    {
      id: "openai/qwen3.8-max",
      name: "Qwen3.8 Max",
      variants: ["none", "low", "medium", "xhigh"],
      default_variant: "xhigh",
    },
    {
      id: "openai/qwen3.8-flash",
      name: "Qwen3.8 Flash",
      variants: ["none", "low", "medium", "xhigh"],
      default_variant: "xhigh",
    },
    {
      id: "openai/deepseek-v4-pro",
      name: "DeepSeek V4 Pro",
      variants: ["off", "low", "high", "max"],
      default_variant: "high",
    },
  ],
  default_model: "openai/gemini-3.8-flash",
  video_models: [
    { id: "video-sd-1080p-pro", name: "SD 1080p Pro", channel: "sd2", resolutions: ["1080p"] },
    { id: "wan3.0-video", name: "Wan 3.0", channel: "sd2", resolutions: ["480p", "720p", "1080p"] },
    { id: "MiniMax-H3", name: "MiniMax H3", channel: "sd2", resolutions: ["480p", "512p", "768p", "2k"] },
  ],
  default_video_model: "wan3.0-video",
  default_video_resolution: "720p",
  model_tiers: {
    chat: [
      { tier: "high", model: "openai/qwen3.8-max", variant: "xhigh" },
      { tier: "medium", model: "openai/gemini-3.8-flash", variant: "medium" },
      { tier: "low", model: "openai/qwen3.8-flash", variant: "low" },
    ],
    video: [
      { tier: "high", model: "video-sd-1080p-pro", resolution: "1080p" },
      { tier: "medium", model: "wan3.0-video", resolution: "720p" },
      { tier: "low", model: "MiniMax-H3", resolution: "768p" },
    ],
  },
}

beforeEach(() => {
  useModelChoiceStore.setState({ picked: new Map() })
  useVideoModelChoiceStore.setState({ picked: new Map() })
})

describe("useComposerModels tiers", () => {
  it("reads the deployment defaults as the medium tiers", () => {
    const { result } = renderHook(() => useComposerModels({ config: CONFIG, sessionKey: "s1" }))
    expect(result.current.tiers.activeChat).toBe("medium")
    expect(result.current.tiers.activeVideo).toBe("medium")
  })

  it("a chat tier picks its model and its strength in one gesture", () => {
    const { result } = renderHook(() => useComposerModels({ config: CONFIG, sessionKey: "s1" }))

    act(() => result.current.tiers.pickChat("high"))
    expect(result.current.chat.activeId).toBe("openai/qwen3.8-max")
    expect(result.current.tiers.activeChat).toBe("high")
    // The strength is stored against the tier's model, so it is what the next
    // request carries even though the pick happened before the re-render.
    expect(result.current.reasoning.value).toBe("xhigh")
    expect(result.current.reasoning.activeId).toBe("xhigh")

    act(() => result.current.tiers.pickChat("low"))
    expect(result.current.chat.activeId).toBe("openai/qwen3.8-flash")
    expect(result.current.reasoning.value).toBe("low")
  })

  it("a null tier variant returns the model to its own default", () => {
    const config: AppConfig = {
      ...CONFIG,
      model_tiers: {
        chat: [{ tier: "high", model: "openai/qwen3.8-max", variant: null }],
        video: [],
      },
    }
    const { result } = renderHook(() =>
      useComposerModels({
        config,
        sessionKey: "s1",
        sessionModel: "openai/gemini-3.8-flash",
        sessionVariant: "high",
      }),
    )
    act(() => result.current.tiers.pickChat("high"))
    expect(result.current.reasoning.value).toBeNull()
    expect(result.current.reasoning.activeId).toBeNull()
  })

  it("a video tier picks the pair, and the pair is what identifies the tier", () => {
    const { result } = renderHook(() => useComposerModels({ config: CONFIG, sessionKey: "s1" }))

    act(() => result.current.tiers.pickVideo("low"))
    expect(result.current.video.pending).toBe("MiniMax-H3")
    expect(result.current.video.pendingResolution).toBe("768p")
    expect(result.current.tiers.activeVideo).toBe("low")

    // Same model at another resolution is not the tier any more.
    act(() => result.current.video.pick("wan3.0-video", "1080p"))
    expect(result.current.tiers.activeVideo).toBeUndefined()
  })

  it("a session pinned outside the tiers matches none, and stays pinned", () => {
    const { result } = renderHook(() =>
      useComposerModels({ config: CONFIG, sessionKey: "s1", sessionModel: "openai/deepseek-v4-pro" }),
    )
    expect(result.current.tiers.activeChat).toBeUndefined()
    expect(result.current.chat.activeId).toBe("openai/deepseek-v4-pro")
  })

  it("an unknown tier is ignored rather than sending a blank model", () => {
    const { result } = renderHook(() => useComposerModels({ config: CONFIG, sessionKey: "s1" }))
    act(() => result.current.tiers.pickChat("high"))
    const before = result.current.chat.activeId
    act(() => result.current.tiers.pickVideo("high"))
    const configWithoutLow: AppConfig = {
      ...CONFIG,
      model_tiers: { chat: CONFIG.model_tiers!.chat.slice(0, 2), video: [] },
    }
    const second = renderHook(() => useComposerModels({ config: configWithoutLow, sessionKey: "s2" }))
    act(() => second.result.current.tiers.pickChat("low"))
    expect(second.result.current.chat.activeId).toBe("openai/gemini-3.8-flash")
    expect(before).toBe("openai/qwen3.8-max")
  })

  it("no declared tiers leaves the hook on the plain pickers", () => {
    const { result } = renderHook(() =>
      useComposerModels({ config: { ...CONFIG, model_tiers: undefined }, sessionKey: "s1" }),
    )
    expect(result.current.tiers.chat).toEqual([])
    expect(result.current.tiers.video).toEqual([])
    expect(result.current.tiers.activeChat).toBeUndefined()
  })
})

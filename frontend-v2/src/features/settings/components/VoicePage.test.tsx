import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { DEFAULT_ASSISTANT_PROFILE } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { toast } from "@/shared/ui/Toast"
import type { AssistantVoices } from "../api/voice"

const mutate = vi.fn()
const play = vi.fn(() => Promise.resolve())
const pause = vi.fn()
const data: AssistantVoices = {
  default: "Serena",
  selected: "Serena",
  voices: [
    {
      id: "Tina",
      name: "甜甜",
      gender: "female",
      lang: "zh",
      description: "甜暖亲切",
      description_en: "Sweet",
    },
    {
      id: "Serena",
      name: "苏瑶",
      gender: "female",
      lang: "zh",
      description: "温柔小姐姐",
      description_en: "Gentle",
    },
    {
      id: "Andre",
      name: "安德雷",
      gender: "male",
      lang: "zh",
      description: "沉稳",
      description_en: "Steady",
    },
    {
      id: "Jennifer",
      name: "詹妮弗",
      gender: "female",
      lang: "en",
      description: "美式英语",
      description_en: "American",
    },
  ],
}

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => (opts?.name ? `${key}:${opts.name}` : key),
    i18n: { language: "zh-CN" },
  }),
}))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))
vi.mock("../api/voice", () => ({
  useAssistantVoices: () => ({ data, isError: false }),
  useChooseVoice: () => ({ mutate, isPending: false }),
  voiceSampleUrl: (id: string) => `/api/assistant/voice/samples/${id}`,
}))

import { VoicePage } from "./VoicePage"

beforeEach(() => {
  vi.stubGlobal(
    "Audio",
    vi.fn(function (this: Record<string, unknown>, src: string) {
      Object.assign(this, { src, play, pause, onended: null })
    }),
  )
})

afterEach(() => {
  cleanup()
  mutate.mockClear()
  play.mockClear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  useAppearanceStore.setState({ assistant: DEFAULT_ASSISTANT_PROFILE })
})

describe("VoicePage", () => {
  it("groups the voices, marks the current and default one, and saves a pick", () => {
    render(<VoicePage />)
    expect(screen.getByText("voice.group.zhFemale")).toBeTruthy()
    expect(screen.getByText("voice.group.zhMale")).toBeTruthy()
    expect(screen.getByText("voice.group.en")).toBeTruthy()
    const serena = screen.getByRole("button", { name: /^苏瑶/ })
    expect(serena.getAttribute("aria-pressed")).toBe("true")
    expect(screen.getByText("voice.defaultTag")).toBeTruthy()
    fireEvent.click(serena)
    expect(mutate).not.toHaveBeenCalled() // already the voice in use
    fireEvent.click(screen.getByRole("button", { name: /^安德雷/ }))
    expect(mutate).toHaveBeenCalledWith("Andre", expect.anything())
  })

  it("plays a short preview of a voice and stops it on a second press", () => {
    render(<VoicePage />)
    const listen = screen.getByRole("button", { name: "voice.preview:甜甜" })
    fireEvent.click(listen)
    expect(Audio).toHaveBeenCalledWith("/api/assistant/voice/samples/Tina")
    expect(play).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("button", { name: "voice.preview:甜甜" }).textContent).toBe("voice.stop")
    fireEvent.click(screen.getByRole("button", { name: "voice.preview:甜甜" }))
    expect(pause).toHaveBeenCalled()
    expect(screen.getByRole("button", { name: "voice.preview:甜甜" }).textContent).toBe("voice.listen")
  })

  it("saves each call habit at once; a call in progress follows it", async () => {
    const put = vi.spyOn(http, "put").mockImplementation(async (_url, body) => ({
      ...DEFAULT_ASSISTANT_PROFILE, ...(body as object),
    }))
    render(<VoicePage />)
    const recap = screen.getByRole("switch", { name: /voice\.call\.recap/ })
    expect(recap.getAttribute("aria-checked")).toBe("true")
    fireEvent.click(recap)
    await waitFor(() => expect(put).toHaveBeenCalledWith("/api/assistant/profile", { call_recap: false }))
    await waitFor(() => expect(useAppearanceStore.getState().assistant.call_recap).toBe(false))
    expect(toast).toHaveBeenCalledWith("success", "voice.call.saved")
    fireEvent.click(screen.getByRole("button", { name: "voice.call.detailOption.detailed" }))
    await waitFor(() => expect(put).toHaveBeenLastCalledWith("/api/assistant/profile", { call_detail: "detailed" }))
  })
})

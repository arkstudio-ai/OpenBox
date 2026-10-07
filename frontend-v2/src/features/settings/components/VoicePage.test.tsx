import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
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

vi.mock("react-i18next", () => ({
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
})

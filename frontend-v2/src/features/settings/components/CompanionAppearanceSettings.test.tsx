import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { createInstance } from "i18next"
import { I18nextProvider } from "react-i18next"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import zh from "@/locales/zh-CN/companion.json"
import en from "@/locales/en-US/companion.json"
import zhSettings from "@/locales/zh-CN/settings.json"
import enSettings from "@/locales/en-US/settings.json"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import { CompanionAvatar } from "@/shared/appearance/CompanionAvatar"
import {
  DEFAULT_COMPANION,
  useCompanionAppearance,
  type CompanionAppearance,
  type CompanionPresetOption,
} from "@/shared/appearance/companion"
import { COMPANION_PRESETS, companionPortrait } from "@/shared/appearance/companion-presets"
import { CompanionAppearanceSettings } from "./CompanionAppearanceSettings"
import { AssistantQuickSettings } from "./AssistantQuickSettings"
import type { AssistantVoices } from "../api/voice"

vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const presets: CompanionPresetOption[] = COMPANION_PRESETS.map((id, index) => {
  const male = [2, 4, 6, 8, 11, 12, 14, 15, 18, 24, 26, 29, 31].includes(index + 1)
  const pet = [8, 10, 11, 12, 13, 14, 21, 29, 31, 33].includes(index + 1)
  return { id, category: pet ? "pets" : male ? "male" : "female", voice_gender: male ? "male" : "female" }
})

beforeEach(() => {
  useAuthStore.getState().setAuth("test", { id: "portrait-owner", username: "owner", role: "user" })
})
afterEach(() => {
  cleanup()
  useAuthStore.getState().clearAuth()
  vi.restoreAllMocks()
})

function OtherSurface() {
  const appearance = useCompanionAppearance().data ?? DEFAULT_COMPANION
  return (
    <div data-testid="other-surface">
      <CompanionAvatar appearance={appearance} />
    </div>
  )
}

async function page(compact = false, lng = "zh-CN", quick = false) {
  const i18n = createInstance()
  await i18n.init({
    lng,
    resources: {
      "zh-CN": { companion: zh, settings: zhSettings },
      "en-US": { companion: en, settings: enSettings },
    },
    defaultNS: "companion",
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          {quick ? (
            <AssistantQuickSettings inCall={false} onNavigate={() => undefined} />
          ) : (
            <CompanionAppearanceSettings compact={compact} />
          )}
          <OtherSurface />
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

it("offers all supplied characters, filters them, and synchronizes a saved choice across surfaces and refresh", async () => {
  let stored = { ...DEFAULT_COMPANION, presets }
  vi.spyOn(http, "get").mockImplementation(async () => stored)
  const put = vi
    .spyOn(http, "put")
    .mockImplementation(async (_url, patch) => (stored = { ...stored, ...(patch as object) }))
  const first = await page()
  const gallery = screen.getByRole("group", { name: "你的助手形象" })
  await waitFor(() =>
    expect(within(gallery).getByRole("button", { name: "制服少女" }).hasAttribute("disabled")).toBe(false),
  )
  expect(within(gallery).getAllByRole("button")).toHaveLength(34)
  for (const preset of COMPANION_PRESETS) {
    const choice = within(gallery).getByRole("button", { name: zh.appearance[preset] })
    expect(choice.querySelector("img")?.getAttribute("src")).toBe(companionPortrait(preset))
    expect(en.appearance[preset]).toBeTruthy()
  }
  fireEvent.click(screen.getByRole("button", { name: "女性" }))
  expect(within(gallery).getAllByRole("button")).toHaveLength(17)
  fireEvent.click(screen.getByRole("button", { name: "男性" }))
  expect(within(gallery).getAllByRole("button")).toHaveLength(7)
  fireEvent.click(screen.getByRole("button", { name: "动物" }))
  expect(within(gallery).getAllByRole("button")).toHaveLength(10)
  expect(within(gallery).getByRole("button", { name: "小羊老师" }).textContent).toContain("默认女声")
  expect(within(gallery).getByRole("button", { name: "邮差金毛" }).textContent).toContain("默认男声")
  fireEvent.click(within(gallery).getByRole("button", { name: "邮差金毛" }))
  await waitFor(() => expect(put).toHaveBeenCalledWith("/api/assistant/appearance", { preset: "ip-29" }))
  await screen.findByText("当前形象：邮差金毛")
  expect(screen.getByTestId("other-surface").querySelector("img")?.getAttribute("src")).toBe(
    companionPortrait("ip-29"),
  )
  first.unmount()
  await page()
  await screen.findByText("当前形象：邮差金毛")
  expect(screen.getByRole("button", { name: "邮差金毛" }).getAttribute("aria-pressed")).toBe("true")
})

it("preserves a failed selection and keeps a saved custom portrait selectable in the compact picker", async () => {
  vi.spyOn(http, "get").mockResolvedValue({
    ...DEFAULT_COMPANION,
    presets,
    preset: "ip-12",
    has_image: true,
    image_url: "https://storage.invalid/portrait",
  })
  vi.spyOn(http, "put").mockRejectedValue(new Error("offline"))
  await page(true, "en-US")
  await screen.findByText("Current look: Bow-tie Tabby")
  const gallery = screen.getByRole("group", { name: "Your assistant's look" })
  expect(within(gallery).getAllByRole("button")).toHaveLength(35)
  expect(
    within(gallery).getByRole("button", { name: "My picture" }).querySelector("img")?.getAttribute("src"),
  ).toBe("https://storage.invalid/portrait")
  fireEvent.click(within(gallery).getByRole("button", { name: "Coffee Cat" }))
  await waitFor(() =>
    expect(within(gallery).getByRole("button", { name: "Coffee Cat" }).hasAttribute("disabled")).toBe(false),
  )
  expect(screen.getByText("Current look: Bow-tie Tabby")).toBeTruthy()
  expect(within(gallery).getByRole("button", { name: "Bow-tie Tabby" }).getAttribute("aria-pressed")).toBe(
    "true",
  )
})

it("refreshes automatic voices after appearance saves, supports a same-default manual pick, and preserves it for custom images", async () => {
  const model = "qwen-audio-3.1-realtime-plus"
  let stored: CompanionAppearance = {
    ...DEFAULT_COMPANION,
    presets,
    has_image: true,
    image_url: "https://storage.invalid/custom",
  }
  let manual: string | undefined
  const voiceState = (): AssistantVoices => {
    const defaultVoice =
      presets.find((p) => p.id === stored.preset)?.voice_gender === "male"
        ? "longanlufeng"
        : "longanqian_v3.1"
    return {
      model,
      default_model: model,
      models: [{ id: model, name: "Audio 3.1", tier: "expert" }],
      default: defaultVoice,
      selected: manual ?? defaultVoice,
      voice_source: manual ? "user" : "appearance",
      voices: [
        {
          id: "longanqian_v3.1",
          name: "龙安浅",
          gender: "female",
          lang: "zh",
          description: "女声",
          description_en: "Female",
        },
        {
          id: "longanlufeng",
          name: "龙安鲁风",
          gender: "male",
          lang: "zh",
          description: "男声",
          description_en: "Male",
        },
      ],
    }
  }
  vi.spyOn(http, "get").mockImplementation(async (url) =>
    url === "/api/assistant/appearance" ? stored : voiceState(),
  )
  const put = vi.spyOn(http, "put").mockImplementation(async (url, body) => {
    if (url === "/api/assistant/appearance") return (stored = { ...stored, ...(body as object) })
    manual = (body as { voice: string }).voice
    return voiceState()
  })
  const first = await page(true, "zh-CN", true)
  const voice = screen.getByRole("combobox", { name: "声音" })
  await screen.findByRole("option", { name: "自动匹配 · 龙安浅" })
  fireEvent.click(screen.getByRole("button", { name: "咖啡橘猫" }))
  await screen.findByRole("option", { name: "自动匹配 · 龙安鲁风" })
  fireEvent.click(screen.getByRole("button", { name: "我的形象" }))
  await screen.findByRole("option", { name: "自动匹配 · 龙安浅" })
  fireEvent.click(screen.getByRole("button", { name: "牛仔伙伴" }))
  await screen.findByRole("option", { name: "自动匹配 · 龙安鲁风" })
  fireEvent.change(voice, { target: { value: "longanlufeng" } })
  await waitFor(() =>
    expect(put).toHaveBeenLastCalledWith("/api/assistant/voice/voice", { model, voice: "longanlufeng" }),
  )
  await waitFor(() => expect(screen.queryByRole("option", { name: /自动匹配/ })).toBeNull())
  for (const name of ["银发西装", "我的形象"]) {
    fireEvent.click(screen.getByRole("button", { name }))
    await screen.findByText(`当前形象：${name}`)
    await waitFor(() => expect(screen.getByRole("button", { name }).hasAttribute("disabled")).toBe(false))
    expect((voice as HTMLSelectElement).value).toBe("longanlufeng")
  }
  first.unmount()
  await page(true, "zh-CN", true)
  await screen.findByText("当前形象：我的形象")
  await waitFor(() =>
    expect((screen.getByRole("combobox", { name: "声音" }) as HTMLSelectElement).value).toBe("longanlufeng"),
  )
})

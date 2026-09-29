import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { createInstance } from "i18next"
import { I18nextProvider } from "react-i18next"
import type { AppConfig } from "@/shared/types/api"
import en from "@/locales/en-US/chat.json"
import zh from "@/locales/zh-CN/chat.json"
import { ModelBadge } from "./MetaBadges"

const query = vi.hoisted(() => ({
  model: undefined as string | undefined,
  config: undefined as Partial<AppConfig> | undefined,
}))

vi.mock("../../api/config", () => ({ useConfigQuery: () => ({ data: query.config }) }))
vi.mock("../../api/message-actions", () => ({
  useSessionQuery: () => ({ data: query.model ? { model: query.model } : undefined }),
}))

const i18n = createInstance()
await i18n.init({
  lng: "zh-CN",
  resources: { "zh-CN": { chat: zh }, "en-US": { chat: en } },
  interpolation: { escapeValue: false },
})

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN")
  query.model = "openai/gemini-3.8-flash-high"
  query.config = {
    models: [{ id: query.model, name: "Gemini 3.8 Flash High" }],
    model_tiers: {
      chat: [
        { tier: "high", model: "openai/deep-model", variant: null },
        { tier: "medium", model: query.model, variant: null },
        { tier: "low", model: "openai/fast-model", variant: null },
      ],
      video: [],
    },
  }
})
afterEach(cleanup)

function renderBadge() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ModelBadge sessionId="session-1" />
    </I18nextProvider>,
  )
}

it.each([
  ["zh-CN", "openai/deep-model", "深度"],
  ["zh-CN", "openai/gemini-3.8-flash-high", "专业"],
  ["zh-CN", "openai/fast-model", "快速"],
  ["en-US", "openai/deep-model", "Deep"],
  ["en-US", "openai/gemini-3.8-flash-high", "Pro"],
  ["en-US", "openai/fast-model", "Fast"],
])("shows the composer tier in %s for %s", async (language, model, label) => {
  await i18n.changeLanguage(language)
  query.model = model
  renderBadge()

  const badge = screen.getByLabelText(i18n.t("chat:meta.model"))
  expect(badge.textContent).toBe(label)
  fireEvent.mouseEnter(badge)
  expect(screen.queryByRole("tooltip")).toBeNull()
  expect(document.body.textContent).not.toContain(model)
  expect(screen.queryByText("Gemini 3.8 Flash High")).toBeNull()
})

it.each(["loading", "missing-session", "no-tiers", "unmapped-model"])(
  "does not expose a model name when %s",
  (state) => {
    if (state === "loading") query.config = undefined
    if (state === "missing-session") query.model = undefined
    if (state === "no-tiers") query.config = { models: query.config?.models }
    if (state === "unmapped-model") query.model = "openai/retired-model"
    renderBadge()

    expect(screen.queryByLabelText(zh.meta.model)).toBeNull()
    expect(document.body.textContent).toBe("")
  },
)

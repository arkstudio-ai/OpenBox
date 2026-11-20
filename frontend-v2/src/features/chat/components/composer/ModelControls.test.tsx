import { fireEvent, render, screen } from "@testing-library/react"
import { expect, it, vi } from "vitest"
import type { AppConfig } from "@/shared/types/api"
import { useComposerModels } from "../../hooks/useComposerModels"
import { ModelControls } from "./ModelControls"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string, values?: { input: string; output: string }) =>
    values ? `${values.input} / ${values.output}` : key }),
}))

vi.mock("@/shared/api/auth-store", () => ({
  useAuthStore: (select: (state: { user: { role: string } }) => unknown) =>
    select({ user: { role: "admin" } }),
}))

it("shows only public names and credit prices in the personal assistant picker", () => {
  const config: AppConfig = {
    default_model: "openai/gpt-6.2-sol",
    models: [{ id: "openai/gpt-6.2-sol", name: "GPT-6.2 Sol", variants: ["xhigh"] }],
    model_tiers: {
      product_chat: true,
      chat: [
        { tier: "standard", model: "openai/gpt-6.2-sol", variant: "xhigh", prices: { input: "2", output: "10" } },
        { tier: "creative", model: "openai/gemini-4-neon-xhigh", variant: "xhigh" },
      ],
      video: [],
    },
  }
  function AssistantModelPicker() {
    const choices = useComposerModels({ config, sessionKey: "assistant" })
    return <ModelControls choices={choices} chatOnly />
  }

  render(<AssistantModelPicker />)
  fireEvent.click(screen.getByTitle("tier.chat.pick"))
  expect(screen.getByText("2 / 10")).toBeTruthy()
  expect(screen.queryByText(/GPT-6\.2|gemini-4|tier.more/)).toBeNull()
  fireEvent.click(screen.getByRole("menuitemradio", { name: "tier.chat.creative" }))
  expect(screen.getByTitle("tier.chat.pick").textContent).toContain("tier.chat.creative")
})

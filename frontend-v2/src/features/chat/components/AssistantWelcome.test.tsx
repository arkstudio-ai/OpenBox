import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { DEFAULT_ASSISTANT_PROFILE } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { AssistantWelcome } from "./AssistantWelcome"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string, options?: { name?: string }) =>
    options?.name ? `${key}:${options.name}` : key }),
}))
afterEach(() => {
  cleanup()
  useAppearanceStore.setState({ assistant: DEFAULT_ASSISTANT_PROFILE })
})

it("greets the user the way they asked to be called, introduces itself by its name and offers six things", () => {
  useAuthStore.setState({ user: { id: "u", username: "memoryqa_2026", role: "user" } as never })
  useAppearanceStore.setState({ assistant: { ...DEFAULT_ASSISTANT_PROFILE, name: "小七", address: "老王" } })
  render(<AssistantWelcome onPick={vi.fn()} />)
  expect(screen.getByRole("heading").textContent).toMatch(/^assistant\.welcome\.greeting\.(morning|afternoon|evening):老王$/)
  expect(screen.getByText("assistant.welcome.introNamed:小七")).toBeTruthy()
  expect(screen.getAllByRole("button")).toHaveLength(6)
})

it("never greets with a sign-in name, and uses the default introduction until the assistant is named", () => {
  useAuthStore.setState({ user: { id: "u", username: "memoryqa_2026", role: "user" } as never })
  render(<AssistantWelcome onPick={vi.fn()} />)
  expect(screen.getByRole("heading").textContent).toMatch(/^assistant\.welcome\.greetingPlain\.(morning|afternoon|evening)$/)
  expect(screen.getByText("assistant.welcome.intro")).toBeTruthy()
})

it("puts an idea into the composer instead of sending it", () => {
  const onPick = vi.fn()
  render(<AssistantWelcome onPick={onPick} />)
  fireEvent.click(screen.getByText("assistant.welcome.ideas.briefing.title"))
  expect(onPick).toHaveBeenCalledExactlyOnceWith("assistant.welcome.ideas.briefing.prompt")
})

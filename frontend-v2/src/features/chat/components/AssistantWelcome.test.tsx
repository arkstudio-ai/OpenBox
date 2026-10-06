import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { AssistantWelcome } from "./AssistantWelcome"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string, options?: { name?: string }) =>
  options?.name ? `${key}:${options.name}` : key }) }))
afterEach(cleanup)

it("greets the user by name, says what the assistant does and offers six things to try", () => {
  useAuthStore.setState({ user: { id: "u", username: "小王", role: "user" } as never })
  render(<AssistantWelcome onPick={vi.fn()} />)
  expect(screen.getByRole("heading").textContent).toMatch(/^assistant\.welcome\.greeting\.(morning|afternoon|evening):小王$/)
  expect(screen.getByText("assistant.welcome.intro")).toBeTruthy()
  expect(screen.getAllByRole("button")).toHaveLength(6)
})

it("puts an idea into the composer instead of sending it", () => {
  const onPick = vi.fn()
  render(<AssistantWelcome onPick={onPick} />)
  fireEvent.click(screen.getByText("assistant.welcome.ideas.briefing.title"))
  expect(onPick).toHaveBeenCalledExactlyOnceWith("assistant.welcome.ideas.briefing.prompt")
})

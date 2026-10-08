import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { DEFAULT_ASSISTANT_PROFILE } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { EmptyState } from "./EmptyState"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, options?: { name?: string; returnObjects?: boolean }) =>
      options?.returnObjects ? [] : options?.name ? `${key}:${options.name}` : key,
  }),
}))
afterEach(() => {
  cleanup()
  useAppearanceStore.setState({ assistant: DEFAULT_ASSISTANT_PROFILE })
})

it("greets a new chat the way the person asked to be called, never with a sign-in name", () => {
  useAuthStore.setState({ user: { id: "u", username: "memoryqa_2026", role: "user" } as never })
  render(<EmptyState onPick={vi.fn()} />)
  expect(screen.getByRole("heading").textContent).toMatch(/^greetingPlain\.(morning|afternoon|evening)$/)
  cleanup()
  useAppearanceStore.setState({ assistant: { ...DEFAULT_ASSISTANT_PROFILE, address: "老王" } })
  render(<EmptyState onPick={vi.fn()} />)
  expect(screen.getByRole("heading").textContent).toMatch(/^greeting\.(morning|afternoon|evening):老王$/)
})

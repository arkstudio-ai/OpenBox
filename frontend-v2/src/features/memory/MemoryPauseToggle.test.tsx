import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { MemoryPauseToggle } from "./MemoryPauseToggle"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh-CN" } }),
}))

beforeEach(() => {
  useAuthStore.setState({ user: { id: "memory-user" } as never, isAuthenticated: true })
  useWorkspaceStore.setState({ currentId: "workspace-1" })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function mount(sessionId: string | null = "chat-1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryPauseToggle sessionId={sessionId} />
    </QueryClientProvider>,
  )
}

it("keeps one chat out of memory and lets it back in", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ auto_save: true, session_paused: false })
  const put = vi.spyOn(http, "put").mockResolvedValue({ auto_save: true, session_paused: true })
  mount()
  const toggle = await screen.findByRole("button", { name: "memoryPause.pauseHint" })
  expect(toggle.getAttribute("aria-pressed")).toBe("false")
  fireEvent.click(toggle)
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith("/api/memories/settings/sessions/chat-1", { paused: true }),
  )
  expect(
    (await screen.findByRole("button", { name: /memoryPause.pausedLabel/ })).getAttribute("aria-pressed"),
  ).toBe("true")
})

it("shows memory is off for the whole account instead of a per-chat switch", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ auto_save: false, session_paused: false })
  mount()
  expect(await screen.findByText("memoryPause.accountOff")).toBeTruthy()
  expect(screen.queryByRole("button")).toBeNull()
})

it("shows nothing outside a chat", () => {
  const get = vi.spyOn(http, "get")
  mount(null)
  expect(screen.queryByRole("button")).toBeNull()
  expect(get).not.toHaveBeenCalled()
})

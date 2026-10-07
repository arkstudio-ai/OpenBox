import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { initialCall } from "../lib/reducer"
import { useVoiceStore } from "../store"
import { VoiceCallButton } from "./VoiceCallButton"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))

const pristine = useVoiceStore.getState()
let config: Record<string, unknown>

beforeEach(() => {
  config = { models: [] }
  useAuthStore.setState({ accessToken: "token", user: { id: "voice-user" } as never, isAuthenticated: true })
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(config), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useVoiceStore.setState(pristine, true)
  useAuthStore.setState({ accessToken: null, user: null, isAuthenticated: false })
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <VoiceCallButton />
    </QueryClientProvider>,
  )
}

it.each([
  ["off", { voice_enabled: false }],
  ["absent", {}],
])("is not offered when voice calls are %s", async (_name, flags) => {
  config = { models: [], ...flags }
  mount()
  await waitFor(() =>
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/agent/config"), expect.anything()),
  )
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(screen.queryByRole("button")).toBeNull()
})

it("dials from the click when voice calls are on", async () => {
  config = { models: [], voice_enabled: true }
  const start = vi.fn()
  useVoiceStore.setState({ start })
  mount()
  const button = await screen.findByRole("button", { name: "button.startLabel" })
  expect(button.textContent).toBe("button.start")
  fireEvent.click(button)
  expect(start).toHaveBeenCalledTimes(1)
})

it("reads 通话中 during a call and brings the window back instead of dialling again", async () => {
  const start = vi.fn()
  useVoiceStore.setState({
    start,
    expanded: false,
    call: { ...initialCall, status: "connected", startedAt: 1 },
  })
  mount()
  const button = screen.getByRole("button", { name: "button.inCall" })
  expect(button.className).toContain("bg-s100")
  fireEvent.click(button)
  expect(useVoiceStore.getState().expanded).toBe(true)
  expect(start).not.toHaveBeenCalled()
})

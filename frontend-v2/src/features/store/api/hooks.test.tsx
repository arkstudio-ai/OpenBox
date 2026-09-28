// Starter cards for the empty page: the store's own once it has one and the
// cards have loaded, null otherwise so the page keeps its locale copy.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { renderHook, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { AuthUser } from "@/shared/types/api"
import { useStarterSuggestions } from "./hooks"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
vi.mock("@/shared/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/http")>()
  return { ...actual, http: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }
})

const cards = [{ title: "Make a video for 招牌菜", hint: "30s" }]

function respond(withStore: boolean) {
  vi.mocked(http.get).mockImplementation(async (path: string) => {
    if (path === "/api/stores") {
      return {
        items: withStore ? [{ id: "st1", personaStatus: "active" }] : [],
        categories: [],
        openCategories: [],
        platforms: [],
      }
    }
    if (path.startsWith("/api/stores/st1/starter-cards")) return { items: cards, personaStatus: "active" }
    throw new Error(`unexpected ${path}`)
  })
}

const clients: QueryClient[] = []
function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  useAuthStore.setState({ user: { id: "u1", username: "u1", role: "user" } as AuthUser })
  useWorkspaceStore.setState({ currentId: "ws", items: [] })
})

afterEach(() => {
  clients.splice(0).forEach((client) => client.clear())
  vi.resetAllMocks()
})

describe("useStarterSuggestions", () => {
  it("returns the store's cards in the viewer's language", async () => {
    respond(true)
    const { result } = renderHook(() => useStarterSuggestions(), { wrapper })
    expect(result.current).toBeNull()
    await waitFor(() => expect(result.current).toEqual(cards))
    expect(http.get).toHaveBeenCalledWith("/api/stores/st1/starter-cards?locale=en-US", expect.anything())
  })

  it("returns null without a store and never asks for cards", async () => {
    respond(false)
    const { result } = renderHook(() => useStarterSuggestions(), { wrapper })
    await waitFor(() => expect(http.get).toHaveBeenCalledWith("/api/stores", expect.anything()))
    await waitFor(() => expect(vi.mocked(http.get).mock.calls.length).toBe(1))
    expect(result.current).toBeNull()
  })
})

import type { ReactNode } from "react"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import { useManageBilling, useWorkspaceBilling } from "./admin-billing"

vi.mock("@/shared/api/http", async (original) => ({
  ...(await original<typeof import("@/shared/api/http")>()),
  http: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}))
const clients: QueryClient[] = []
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={clients.at(-1)!}>{children}</QueryClientProvider>
}
function setup() {
  clients.push(new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  useAuthStore.setState({
    user: { id: "operator", role: "admin" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>,
  })
}
afterEach(() => {
  cleanup()
  clients.splice(0).forEach((c) => c.clear())
  vi.resetAllMocks()
})

it("opening billing details performs no credit or subscription write", async () => {
  setup()
  vi.mocked(http.get).mockResolvedValue({ balance: "10" })
  renderHook(() => useWorkspaceBilling("target"), { wrapper })
  await waitFor(() => expect(http.get).toHaveBeenCalledOnce())
  expect(http.post).not.toHaveBeenCalled()
  expect(http.patch).not.toHaveBeenCalled()
})

it("refuses a prepared mutation when admin identity changes before submission", async () => {
  setup()
  const { result } = renderHook(() => useManageBilling("target"), { wrapper })
  const submit = result.current.mutateAsync
  act(() =>
    useAuthStore.setState({
      user: { id: "other", role: "admin" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>,
    }),
  )
  await act(async () => {
    await expect(
      submit({
        actorId: "operator",
        workspaceId: "target",
        kind: "credits",
        body: { request_key: "request-key", reason: "support", credits: "1" },
      }),
    ).rejects.toMatchObject({ status: 403 })
  })
  expect(http.post).not.toHaveBeenCalled()
})

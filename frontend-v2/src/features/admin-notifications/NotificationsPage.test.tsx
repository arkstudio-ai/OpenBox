import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { http, ApiError } from "@/shared/api/http"
import { NotificationsPage } from "./NotificationsPage"
import type { PushOverview } from "./api"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: "zh-CN", exists: () => true } }),
}))
const snapshot = (): PushOverview => ({
  device: {
    registered: true,
    bindingId: "phone-a",
    platform: "ios",
    provider: "apns",
    appVersion: "1",
    apnsEnvironment: "sandbox",
    notificationsEnabled: true,
    ready: true,
  },
  presence: { appState: "background", pushAllowed: true },
  providers: [{ id: "apns", configured: true }],
  templates: [{ id: "system_test", title: "通知测试", body: "手机通知正常。" }],
  tests: [],
  delaySeconds: 10,
  cooldownSeconds: 30,
})
let client: QueryClient
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useAuthStore.setState({
    user: { id: "admin-a", username: "Admin", role: "admin" } as NonNullable<
      ReturnType<typeof useAuthStore.getState>["user"]
    >,
  })
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  useAuthStore.getState().clearAuth()
})
const mount = () =>
  render(
    <QueryClientProvider client={client}>
      <NotificationsPage />
    </QueryClientProvider>,
  )

it.each(["user", "owner"])("does not fetch or show test controls for %s", async (role) => {
  useAuthStore.setState({ user: { ...useAuthStore.getState().user!, role } })
  const get = vi.spyOn(http, "get")
  mount()
  expect(screen.getByText("mobile.forbidden")).toBeTruthy()
  expect(get).not.toHaveBeenCalled()
  expect(screen.queryByRole("button", { name: "push.send" })).toBeNull()
})

it("targets the current binding, prevents double sends and reuses request ID after network failure", async () => {
  vi.spyOn(http, "get").mockResolvedValue(snapshot())
  let reject!: (error: Error) => void
  const post = vi.spyOn(http, "post").mockImplementationOnce(
    () =>
      new Promise((_, no) => {
        reject = no
      }),
  )
  mount()
  const send = await screen.findByRole("button", { name: "push.send" })
  fireEvent.click(send)
  fireEvent.click(send)
  expect(post).toHaveBeenCalledTimes(1)
  const request = post.mock.calls[0]![1] as Record<string, string>
  expect(post.mock.calls[0]![0]).toBe("/api/admin/push/test")
  expect(request).toEqual({ template: "system_test", bindingId: "phone-a", requestId: expect.any(String) })
  await act(async () => reject(new Error("offline")))
  post.mockResolvedValueOnce({})
  fireEvent.click(screen.getByRole("button", { name: "push.send" }))
  await screen.findByText("push.queued")
  expect(post.mock.calls[1]![1]).toEqual(request)
})

it("distinguishes provider acceptance from phone receipt, disables an unready phone, and hides a revoked role", async () => {
  const data = snapshot()
  data.device.ready = false
  data.tests = [
    {
      id: "one",
      template: "system_test",
      title: "通知测试",
      body: "手机通知正常。",
      status: "accepted",
      error: null,
      receipt: null,
      receiptAt: null,
      attempts: 1,
      availableAt: null,
      createdAt: "2026-09-10T00:00:00Z",
      expiresAt: "2026-09-10T00:05:00Z",
    },
  ]
  const get = vi.spyOn(http, "get").mockResolvedValue(data)
  mount()
  expect(await screen.findByText("push.status.accepted")).toBeTruthy()
  expect((screen.getByRole("button", { name: "push.send" }) as HTMLButtonElement).disabled).toBe(true)
  get.mockResolvedValue({ ...data, tests: [{ ...data.tests[0], receipt: "opened" }] })
  fireEvent.click(screen.getByRole("button", { name: "push.refresh" }))
  await screen.findByText("push.status.opened")
  get.mockRejectedValue(new ApiError(403, "FORBIDDEN", "Forbidden"))
  await waitFor(() =>
    expect((screen.getByRole("button", { name: "push.refresh" }) as HTMLButtonElement).disabled).toBe(false),
  )
  fireEvent.click(screen.getByRole("button", { name: "push.refresh" }))
  await screen.findByText("mobile.forbidden")
  expect(screen.queryByRole("button", { name: "push.send" })).toBeNull()
})

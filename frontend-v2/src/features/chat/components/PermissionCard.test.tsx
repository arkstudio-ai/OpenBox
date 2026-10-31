import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { usePendingStore } from "../stores/pending"
import { PermissionCard } from "./PermissionCard"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  usePendingStore.getState().reset()
})

it.each([
  ["permission.allow", "once"],
  ["permission.allowAlways", "always"],
  ["permission.deny", "reject"],
])("submits %s with the permission service's exact action", async (button, action) => {
  const request = { id: "permission-1", session_id: "session-1", tool: "shell" }
  usePendingStore.getState().addPermission(request)
  const post = vi.spyOn(http, "post").mockResolvedValue({ ok: true })
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <PermissionCard request={request} />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole("button", { name: button }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/agent/permission/permission-1", { action }))
  await waitFor(() => expect(usePendingStore.getState().permissions.get("session-1")).toEqual([]))
  client.clear()
})

it("retires a card whose expires_at has passed instead of offering buttons that would 410", () => {
  const request = { id: "permission-2", session_id: "session-1", tool: "doom_loop",
    expires_at: "2000-01-01T00:00:00Z", assistant: { task_id: "t", workspace_id: "w", assistant_session_id: "a", project_id: "p" } }
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <PermissionCard request={request as never} />
    </QueryClientProvider>,
  )
  expect(screen.getByRole("status").textContent).toBe("permission.expired")
  for (const name of ["permission.allow", "permission.allowAlways", "permission.deny"]) {
    expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true)
  }
  client.clear()
})

it("flips to expired when expires_at passes while the card is on screen", () => {
  vi.useFakeTimers()
  const request = { id: "permission-3", session_id: "session-1", tool: "shell",
    expires_at: new Date(Date.now() + 1_000).toISOString() }
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <PermissionCard request={request} />
    </QueryClientProvider>,
  )
  expect((screen.getByRole("button", { name: "permission.allow" }) as HTMLButtonElement).disabled).toBe(false)
  act(() => { vi.advanceTimersByTime(1_500) })
  expect((screen.getByRole("button", { name: "permission.allow" }) as HTMLButtonElement).disabled).toBe(true)
  vi.useRealTimers()
  client.clear()
})

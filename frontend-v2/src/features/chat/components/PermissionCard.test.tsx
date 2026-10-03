import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
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

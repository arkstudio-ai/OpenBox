import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import AdminAccountsRoute from "./AdminAccountsRoute"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it("submits phone and email separately and can reset the results", async () => {
  const request = vi.spyOn(http, "post")
    .mockResolvedValueOnce({ total: 1, items: [{
      id: "u1", username: "demo-store", phone: "•••• 4567", email: "d••@example.test",
      default_workspace_id: "store",
    }] })
    .mockResolvedValueOnce({ total: 0, items: [] })
  render(<QueryClientProvider client={new QueryClient()}>
    <MemoryRouter><AdminAccountsRoute /></MemoryRouter>
  </QueryClientProvider>)
  const input = screen.getByRole("textbox", { name: "accounts.keyword" })
  const submit = screen.getByRole("button", { name: "accounts.search" })
  expect(request).not.toHaveBeenCalled()
  fireEvent.change(input, { target: { value: " +65 8123 4567 " } })
  fireEvent.click(submit)
  expect(await screen.findByText("demo-store")).toBeTruthy()
  expect(request).toHaveBeenLastCalledWith("/api/admin/users/search", {
    kind: "phone", q: "+65 8123 4567", offset: 0, limit: 20,
  })
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "email" } })
  fireEvent.change(input, { target: { value: "demo@example.test" } })
  fireEvent.click(submit)
  expect(await screen.findByText("accounts.empty")).toBeTruthy()
  expect(request).toHaveBeenLastCalledWith("/api/admin/users/search", {
    kind: "email", q: "demo@example.test", offset: 0, limit: 20,
  })
  fireEvent.click(screen.getByRole("button", { name: "accounts.reset" }))
  expect(screen.getByText("accounts.idle")).toBeTruthy()
})

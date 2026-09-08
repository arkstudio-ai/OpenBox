import { Suspense, useEffect } from "react"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import { MemoryRouter, useLocation } from "react-router"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import type { SubscriptionRow } from "@/features/admin-billing/types"
import { SubscriptionsPage } from "./SubscriptionsPage"

vi.mock("@/shared/api/http", () => ({ http: { get: vi.fn(), post: vi.fn() } }))

const clients: QueryClient[] = []
const here = { pathname: "", search: "" }

function Recorder() {
  const location = useLocation()
  // In an effect, not during render: `fireEvent` flushes effects inside act(),
  // so the assertions still see the address the click produced.
  useEffect(() => {
    here.pathname = location.pathname
    here.search = location.search
  })
  return null
}

function mount(entry = "/app/admin/billing/subscriptions") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <Recorder />
          <Suspense fallback={null}>
            <SubscriptionsPage />
          </Suspense>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

function row(overrides: Partial<SubscriptionRow> = {}): SubscriptionRow {
  return {
    workspace: { id: "ws-1", name: "Acme 空间", kind: "team" },
    owner: { id: "u-1", username: "ada", email: "ada@example.com" },
    plan_id: "pro",
    cycle: "monthly",
    starts_at: "2026-08-01T00:00:00Z",
    ends_at: "2026-09-01T00:00:00Z",
    state: "active",
    queued_count: 2,
    balance: "12.5",
    last_paid_at: "2026-08-01T02:00:00Z",
    ...overrides,
  }
}

function answer(items: SubscriptionRow[], total = items.length) {
  vi.mocked(http.get).mockResolvedValue({ items, total, offset: 0, limit: 25 })
}

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN")
  useAuthStore.setState({
    user: { id: "admin-1", username: "root", role: "admin" } as NonNullable<
      ReturnType<typeof useAuthStore.getState>["user"]
    >,
  })
})

afterEach(() => {
  cleanup()
  clients.splice(0).forEach((client) => client.clear())
  vi.resetAllMocks()
})

describe("SubscriptionsPage", () => {
  it("asks for the first page with no filter keys and renders a row", async () => {
    answer([row()])
    mount()
    await screen.findByText("Acme 空间")
    // "all" is a UI sentinel; sending it as ?plan=all would answer 422.
    expect(http.get).toHaveBeenCalledWith("/api/admin/billing/subscriptions?offset=0&limit=25")
    // Scoped to the table: the filter selects carry the same option labels.
    const table = within(screen.getByRole("table"))
    expect(table.getByText("ws-1")).toBeDefined()
    expect(table.getByText("ada@example.com")).toBeDefined()
    expect(table.getByText("专业版")).toBeDefined()
    expect(table.getByText("月付")).toBeDefined()
    expect(table.getByText("12.5")).toBeDefined()
  })

  it("maps each subscription state onto its own pill", async () => {
    answer([
      row(),
      row({ workspace: { id: "ws-2", name: "Beta", kind: "personal" }, state: "expired" }),
      row({
        workspace: { id: "ws-3", name: "Gamma", kind: "personal" },
        state: "free",
        plan_id: "free",
        cycle: null,
      }),
    ])
    mount()
    await screen.findByText("Acme 空间")
    const table = within(screen.getByRole("table"))
    expect(table.getByText("有效").className).toContain("bg-s100")
    expect(table.getByText("已到期").className).toContain("bg-a200")
    expect(table.getByText("免费").className).toContain("bg-hairsoft")
  })

  it("opens the workspace detail when a row is clicked", async () => {
    answer([row()])
    mount()
    fireEvent.click(await screen.findByText("Acme 空间"))
    expect(here.pathname).toBe("/app/admin/billing/workspaces/ws-1")
  })

  it("applies filters only on submit, and puts them in the URL", async () => {
    answer([row()])
    mount("/app/admin/billing/subscriptions?offset=25")
    await screen.findByText("Acme 空间")
    vi.mocked(http.get).mockClear()

    fireEvent.change(screen.getByRole("combobox", { name: "套餐" }), { target: { value: "pro" } })
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索" }), { target: { value: " acme " } })
    // Nothing has been requested yet: typing must not hit the server.
    expect(http.get).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "查询" }))
    await waitFor(() =>
      expect(http.get).toHaveBeenCalledWith(
        "/api/admin/billing/subscriptions?offset=0&limit=25&plan=pro&q=acme",
      ),
    )
    // A changed filter goes back to page one.
    expect(here.search).toBe("?plan=pro&q=acme")
  })

  it("pages without losing the filters", async () => {
    answer([row()], 60)
    mount("/app/admin/billing/subscriptions?plan=pro")
    await screen.findByText("Acme 空间")
    fireEvent.click(screen.getByRole("button", { name: "下一页" }))
    await waitFor(() =>
      expect(http.get).toHaveBeenCalledWith(
        "/api/admin/billing/subscriptions?offset=25&limit=25&plan=pro",
      ),
    )
    expect(here.search).toBe("?plan=pro&offset=25")
  })

  it("shows the empty state rather than an empty table", async () => {
    answer([])
    mount()
    expect(await screen.findByText("没有匹配的空间。")).toBeDefined()
  })

  it("shows the error state when the list cannot be read", async () => {
    vi.mocked(http.get).mockRejectedValue(new Error("boom"))
    mount()
    expect(await screen.findByRole("alert")).toBeDefined()
  })
})

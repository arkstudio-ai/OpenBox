import { Suspense } from "react"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import { MemoryRouter } from "react-router"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import type { AdminOrderRow } from "@/features/admin-billing/types"
import { OrdersPage } from "./OrdersPage"

vi.mock("@/shared/api/http", () => ({ http: { get: vi.fn(), post: vi.fn() } }))

const clients: QueryClient[] = []

function mount(entry = "/app/admin/billing/orders") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <Suspense fallback={null}>
            <OrdersPage />
          </Suspense>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

function order(overrides: Partial<AdminOrderRow> = {}): AdminOrderRow {
  return {
    id: "ord-1",
    workspace_id: "ws-1",
    workspace_name: "Acme 空间",
    user_id: "u-1",
    user: { id: "u-1", username: "ada", email: "ada@example.com" },
    provider: "alipay",
    kind: "subscription",
    amount_fen: 100000,
    currency: "CNY",
    credits: "500",
    status: "paid",
    plan_id: "pro",
    cycle: "monthly",
    provider_order_id: "2026090812345",
    created_at: "2026-09-08T01:00:00Z",
    paid_at: "2026-09-08T01:02:00Z",
    cancelled_at: null,
    cancellation_reason: null,
    ...overrides,
  }
}

function answer(items: AdminOrderRow[], total = items.length) {
  vi.mocked(http.get).mockImplementation(async (path: string) => {
    if (path.startsWith("/api/billing/providers")) {
      return { items: [{ id: "alipay", name: "支付宝" }] }
    }
    return { items, total, offset: 0, limit: 25 }
  })
}

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN")
  useAuthStore.setState({
    user: { id: "admin-1", username: "root", role: "admin" } as NonNullable<
      ReturnType<typeof useAuthStore.getState>["user"]
    >,
  })
  useWorkspaceStore.setState({ currentId: "ws-admin" })
})

afterEach(() => {
  cleanup()
  clients.splice(0).forEach((client) => client.clear())
  vi.resetAllMocks()
  useWorkspaceStore.setState({ currentId: null })
})

describe("OrdersPage", () => {
  it("renders money from fen, the product, and the channel's display name", async () => {
    answer([order()])
    mount()
    await screen.findByText("ord-1")
    const table = within(screen.getByRole("table"))
    // 100000 fen is ¥1,000.00 — never ¥100000 and never ¥1000.
    expect(table.getByText("¥1,000.00")).toBeDefined()
    expect(table.getByText("专业版 · 月付")).toBeDefined()
    expect(table.getByText("支付宝")).toBeDefined()
    expect(table.getByText("2026090812345")).toBeDefined()
    expect(table.getByText("已支付").className).toContain("bg-s100")
  })

  it("puts a cancelled order's reason on the pill", async () => {
    answer([order({ status: "cancelled", cancellation_reason: "gateway_closed" })])
    mount()
    await screen.findByText("ord-1")
    const pill = within(screen.getByRole("table")).getByText("已取消")
    expect(pill.className).toContain("bg-dangersoft")
    expect(pill.getAttribute("title")).toBe("取消原因：gateway_closed")
  })

  it("sends calendar days and refuses a reversed range before the server does", async () => {
    answer([order()])
    mount()
    await screen.findByText("ord-1")
    vi.mocked(http.get).mockClear()

    fireEvent.change(screen.getByLabelText("开始日期"), { target: { value: "2026-09-10" } })
    fireEvent.change(screen.getByLabelText("结束日期"), { target: { value: "2026-09-01" } })
    const submit = screen.getByRole("button", { name: "查询" }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    expect(screen.getByRole("alert").textContent).toBe("开始日期不能晚于结束日期。")

    fireEvent.change(screen.getByLabelText("结束日期"), { target: { value: "2026-09-30" } })
    fireEvent.click(screen.getByRole("button", { name: "查询" }))
    await waitFor(() =>
      expect(http.get).toHaveBeenCalledWith(
        "/api/admin/billing/orders?offset=0&limit=25&from=2026-09-10&to=2026-09-30",
      ),
    )
  })

  it("shows the empty state", async () => {
    answer([])
    mount()
    expect(await screen.findByText("没有匹配的订单。")).toBeDefined()
  })
})

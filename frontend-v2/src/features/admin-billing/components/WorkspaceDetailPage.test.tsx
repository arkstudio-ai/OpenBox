import { Suspense } from "react"
import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import { MemoryRouter } from "react-router"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError, http } from "@/shared/api/http"
import type { WorkspaceBillingDetail } from "@/features/admin-billing/types"
import { WorkspaceDetailPage } from "./WorkspaceDetailPage"

// Keeps the real ApiError so the 404 branch is exercised through the same class
// the transport actually throws.
vi.mock("@/shared/api/http", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/shared/api/http")>()),
  http: { get: vi.fn(), post: vi.fn() },
}))

const clients: QueryClient[] = []

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <Suspense fallback={null}>
            <WorkspaceDetailPage workspaceId="ws-1" />
          </Suspense>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

function detail(overrides: Partial<WorkspaceBillingDetail> = {}): WorkspaceBillingDetail {
  return {
    workspace: {
      id: "ws-1",
      name: "Acme 空间",
      kind: "team",
      owner_user_id: "u-1",
      plan_id: "pro",
      created_at: "2026-01-01T00:00:00Z",
      is_deleted: false,
      deleted_at: null,
    },
    owner: { id: "u-1", username: "ada", email: "ada@example.com" },
    member_count: 4,
    balance: "12.5",
    plan_id: "pro",
    subscription: {
      order_id: "ord-1",
      plan_id: "pro",
      cycle: "monthly",
      starts_at: "2026-08-01T00:00:00Z",
      ends_at: "2026-09-01T00:00:00Z",
    },
    queued: [
      {
        order_id: "ord-2",
        plan_id: "pro",
        cycle: "monthly",
        starts_at: "2026-09-01T00:00:00Z",
        ends_at: "2026-10-01T00:00:00Z",
      },
    ],
    history: [
      {
        order_id: "ord-2",
        plan_id: "pro",
        cycle: "monthly",
        starts_at: "2026-09-01T00:00:00Z",
        ends_at: "2026-10-01T00:00:00Z",
      },
      {
        order_id: "ord-1",
        plan_id: "pro",
        cycle: "monthly",
        starts_at: "2026-08-01T00:00:00Z",
        ends_at: "2026-09-01T00:00:00Z",
      },
    ],
    orders: [],
    ledger: [
      {
        id: "l-1",
        kind: "grant",
        amount: "500",
        balance_after: "512.5",
        reference_id: "ord-1",
        created_at: "2026-08-01T00:00:00Z",
      },
    ],
    usage: {
      since: "2026-08-09T00:00:00Z",
      days: 30,
      items: [{ status: "charged", events: 12, total_tokens: 3456, credits: "0.25" }],
    },
    ...overrides,
  }
}

function answer(value: WorkspaceBillingDetail) {
  vi.mocked(http.get).mockImplementation(async (path: string) => {
    if (path.startsWith("/api/billing/providers")) return { items: [] }
    return value
  })
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

describe("WorkspaceDetailPage", () => {
  it("shows the header, the four cards and no write action at all", async () => {
    answer(detail())
    const { container } = mount()
    await screen.findByText("Acme 空间")
    expect(screen.getByText("ada@example.com")).toBeDefined()
    expect(screen.getByText("12.5")).toBeDefined()
    expect(screen.getByText("订阅历史")).toBeDefined()
    expect(screen.getByText("订单")).toBeDefined()
    expect(screen.getByText("积分账本")).toBeDefined()
    expect(screen.getByText("近 30 天用量")).toBeDefined()
    expect(screen.getByText("12 次调用")).toBeDefined()
    // §3-Q5: this round has no money writes, so nothing here may be clickable
    // beyond the back link.
    expect(container.querySelectorAll("button")).toHaveLength(0)
  })

  it("marks the live term and the queued renewal", async () => {
    answer(detail())
    mount()
    await screen.findByText("订阅历史")
    const terms = within(screen.getAllByRole("table")[0])
    expect(terms.getByText("当前")).toBeDefined()
    expect(terms.getByText("排队中")).toBeDefined()
  })

  it("keeps a soft-deleted workspace readable and says so", async () => {
    answer(
      detail({
        workspace: {
          ...detail().workspace,
          is_deleted: true,
          deleted_at: "2026-09-01T00:00:00Z",
        },
      }),
    )
    mount()
    expect(await screen.findByText("已删除")).toBeDefined()
  })

  it("separates a missing workspace from a broken request", async () => {
    vi.mocked(http.get).mockRejectedValue(new ApiError(404, "NOT_FOUND", "Workspace not found"))
    mount()
    expect(await screen.findByText("找不到这个空间，它可能从未存在。")).toBeDefined()
    expect(screen.queryByRole("alert")).toBeNull()
  })

  it("shows the error state on any other failure", async () => {
    vi.mocked(http.get).mockRejectedValue(new ApiError(500, "HTTP_500", "boom"))
    mount()
    expect(await screen.findByRole("alert")).toBeDefined()
  })
})

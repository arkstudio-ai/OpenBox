import { Suspense } from "react"
import "@testing-library/jest-dom/vitest"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
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
  http: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
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
  sessionStorage.clear()
})

describe("WorkspaceDetailPage", () => {
  it("keeps views readable when the server does not permit mutations", async () => {
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
    // Old servers and inactive accounts never expose money writes.
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

  it("requires a valid amount, reason and target confirmation before adding exact credits", async () => {
    const current = detail({ can_manage: true })
    answer(current)
    vi.mocked(http.post).mockImplementation(async () => {
      current.balance = "22.625"
      return { operation_id: "receipt", balance: "22.625", subscription: null, replayed: false }
    })
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "充值积分" }))
    const dialog = within(screen.getByRole("dialog"))
    const submit = dialog.getByRole("button", { name: "充值积分" })
    expect(submit).toBeDisabled()
    fireEvent.change(dialog.getByLabelText("充值积分数", { exact: false }), { target: { value: "10.125" } })
    fireEvent.change(dialog.getByLabelText("操作原因（必填）"), { target: { value: "support" } })
    expect(submit).toBeDisabled()
    fireEvent.click(dialog.getByRole("checkbox"))
    fireEvent.click(submit)
    fireEvent.click(submit)
    await screen.findByRole("status")
    expect(http.post).toHaveBeenCalledTimes(1)
    expect(http.post).toHaveBeenCalledWith(
      "/api/admin/billing/workspaces/ws-1/credits",
      expect.objectContaining({ credits: "10.125", reason: "support", request_key: expect.any(String) }),
    )
    expect(await screen.findByText("22.625")).toBeDefined()
  })

  it("keeps an uncertain request key and payload when the operator reopens the dialog", async () => {
    answer(detail({ can_manage: true }))
    vi.mocked(http.post)
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce({ operation_id: "receipt", balance: "112.5", replayed: true })
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "充值积分" }))
    let dialog = within(screen.getByRole("dialog"))
    fireEvent.change(dialog.getByLabelText("充值积分数", { exact: false }), { target: { value: "100" } })
    fireEvent.change(dialog.getByLabelText("操作原因（必填）"), { target: { value: "retry proof" } })
    fireEvent.click(dialog.getByRole("checkbox"))
    fireEvent.click(dialog.getByRole("button", { name: "充值积分" }))
    await dialog.findByRole("alert")
    expect(dialog.getByLabelText("充值积分数", { exact: false })).toBeDisabled()
    const original = vi.mocked(http.post).mock.calls[0]
    fireEvent.click(dialog.getByRole("button", { name: "关闭" }))
    fireEvent.click(screen.getByRole("button", { name: "开通 / 续期" }))
    dialog = within(screen.getByRole("dialog"))
    expect(dialog.getByLabelText("充值积分数", { exact: false })).toHaveValue("100")
    fireEvent.click(dialog.getByRole("button", { name: "确认并重试原操作" }))
    await screen.findByRole("status")
    expect(vi.mocked(http.post).mock.calls[1]).toEqual(original)
    expect(sessionStorage.length).toBe(0)
  })

  it("sends the term revision when changing a subscription and preserves its exact expiry", async () => {
    const term = {
      id: "term-1",
      order_id: null,
      plan_id: "pro",
      cycle: "monthly",
      starts_at: "2026-01-01T00:00:00Z",
      ends_at: "2030-01-01T00:00:30.123Z",
      revision: "a".repeat(64),
    }
    answer(detail({ can_manage: true, subscription: term, history: [term], queued: [] }))
    vi.mocked(http.patch).mockResolvedValue({ operation_id: "receipt" })
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "调整订阅" }))
    const dialog = within(screen.getByRole("dialog"))
    fireEvent.change(dialog.getByLabelText("套餐"), { target: { value: "max" } })
    fireEvent.change(dialog.getByLabelText("操作原因（必填）"), { target: { value: "upgrade" } })
    fireEvent.click(dialog.getByRole("checkbox"))
    fireEvent.click(dialog.getByRole("button", { name: "调整订阅" }))
    await waitFor(() =>
      expect(http.patch).toHaveBeenCalledWith(
        "/api/admin/billing/workspaces/ws-1/subscriptions/term-1",
        expect.objectContaining({ plan_id: "max", expected_revision: term.revision, ends_at: term.ends_at }),
      ),
    )
  })
})

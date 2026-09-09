import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import { UsagePage } from "./UsagePage"
import { CreditTopUp } from "./CreditTopUp"
import { PaymentHistory } from "./PaymentHistory"
import { useCreditBalance } from "@/shared/api/billing"

vi.mock("@/shared/api/http", () => ({ http: { get: vi.fn(), post: vi.fn() } }))
const redirect = vi.hoisted(() => vi.fn())
vi.mock("../hooks/useCheckoutRedirect", () => ({
  useCheckoutRedirect: () => async (prepare: () => Promise<unknown>) => {
    try {
      redirect(await prepare())
    } catch {
      /* The mutation exposes errors to the component. */
    }
  },
}))

const clients: QueryClient[] = []
function BalanceProbe() {
  const balance = useCreditBalance()
  return <output>{`余额：${balance.data?.balance ?? "—"}`}</output>
}
function mount(element = <UsagePage />) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  clients.push(client)
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>{element}</MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN")
  useAuthStore.setState({
    user: { id: "user", username: "tester", role: "user" } as NonNullable<
      ReturnType<typeof useAuthStore.getState>["user"]
    >,
  })
  useWorkspaceStore.setState({ currentId: "workspace" })
  vi.mocked(http.get).mockImplementation(async (path) => {
    if (path.endsWith("/balance")) return { balance: "8.25", mode: "shadow" }
    if (path.endsWith("/summary"))
      return { total_tokens: 123, total_credits: "0.0000002", historical_count: 1, unpriced_count: 0 }
    if (path.endsWith("/providers")) return { items: [] }
    if (path.startsWith("/api/billing/orders?")) return { items: [], page: 1, total: 0, total_pages: 1 }
    const page = new URL(path, "http://test").searchParams.get("page") ?? "1"
    return {
      total: 21,
      total_pages: 2,
      page: Number(page),
      page_size: 20,
      items: [
        {
          id: page,
          session_id: "s" + page,
          session_title: "会话 " + page,
          session_available: true,
          model_id: "openai/gpt-5.6-luna",
          kind: "chat",
          tokens: { input: 1, output: 0, cache: 0 },
          credits: "0.0000002",
          status: "historical",
          created_at: "2026-09-05T01:00:00Z",
        },
      ],
    }
  })
})

afterEach(() => {
  cleanup()
  clients.splice(0).forEach((client) => client.clear())
  vi.resetAllMocks()
  useWorkspaceStore.setState({ currentId: null })
})

describe("credit usage", () => {
  it("shows model, accurate small credit amounts and server pagination without currency", async () => {
    const { container } = mount()
    await screen.findByText("gpt-5.6-luna")
    expect(screen.getByText("8.25 积分")).toBeDefined()
    expect(screen.getByText("0.0000002 积分")).toBeDefined()
    expect(container.textContent).not.toMatch(/US\$|USD|人民币|￥/)
    expect(container.textContent).not.toMatch(/缓存包含在输入|暂未从余额扣除|历史记录按|充值渠道接入后/)
    expect((screen.getByRole("button", { name: "上一页" }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole("button", { name: "下一页" }))
    await screen.findByRole("link", { name: "会话 2" })
    expect(http.get).toHaveBeenCalledWith("/api/billing/usage?page=2&page_size=20", {
      headers: { "X-Workspace-Id": "workspace" },
    })
    expect(screen.queryByRole("link", { name: "会话 1" })).toBeNull()
    fireEvent.change(screen.getByRole("combobox", { name: "每页" }), { target: { value: "10" } })
    await waitFor(() =>
      expect(http.get).toHaveBeenCalledWith("/api/billing/usage?page=1&page_size=10", {
        headers: { "X-Workspace-Id": "workspace" },
      }),
    )
  })

  it("filters totals and details together, resets pagination and restores all dates", async () => {
    const originalGet = vi.mocked(http.get).getMockImplementation()!
    vi.mocked(http.get).mockImplementation(async (path, options) => {
      const url = new URL(path, "http://test")
      if (url.searchParams.has("date_from")) {
        if (url.pathname.endsWith("/summary"))
          return { total_tokens: 45, total_credits: "0.02", historical_count: 0, unpriced_count: 0 }
        if (url.pathname.endsWith("/usage"))
          return {
            items: [
              {
                id: "filtered",
                session_id: "filtered-session",
                session_title: "筛选会话",
                session_available: true,
                model_id: "openai/gpt-5.6-luna",
                kind: "chat",
                tokens: { input: 40, output: 5, cache: 0 },
                credits: "0.02",
                status: "charged",
                created_at: "2026-09-04T01:00:00Z",
              },
            ],
            total: 1,
            total_pages: 1,
            page: 1,
            page_size: 20,
          }
      }
      return originalGet(path, options)
    })
    mount()
    await screen.findByRole("link", { name: "会话 1" })
    fireEvent.click(screen.getByRole("button", { name: "下一页" }))
    await screen.findByRole("link", { name: "会话 2" })
    fireEvent.change(screen.getByLabelText("开始日期"), { target: { value: "2026-09-01" } })
    fireEvent.change(screen.getByLabelText("结束日期"), { target: { value: "2026-09-04" } })
    fireEvent.click(screen.getByRole("button", { name: "筛选" }))
    await screen.findByRole("link", { name: "筛选会话" })
    await screen.findByText("45")
    expect(screen.getByText("0.02")).toBeDefined()
    expect(screen.getByText("8.25 积分")).toBeDefined()
    expect(screen.getByText("所选时段 tokens")).toBeDefined()
    expect(screen.getByText("1 / 1 页")).toBeDefined()
    for (const endpoint of ["summary", "usage"]) {
      const request = vi.mocked(http.get).mock.calls.find(([path]) => {
        const url = new URL(path, "http://test")
        return url.pathname.endsWith("/" + endpoint) && url.searchParams.has("date_from")
      })!
      const params = new URL(request[0], "http://test").searchParams
      expect(params.get("date_from")).toBe("2026-09-01")
      expect(params.get("date_to")).toBe("2026-09-04")
      expect(params.get("tz")).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone)
      if (endpoint === "usage") expect(params.get("page")).toBe("1")
      expect(request[1]).toEqual({ headers: { "X-Workspace-Id": "workspace" } })
    }
    fireEvent.click(screen.getByRole("button", { name: "重置" }))
    await screen.findByRole("link", { name: "会话 1" })
    expect(screen.getByText("累计 tokens")).toBeDefined()
    expect(screen.getByText("123")).toBeDefined()
    expect((screen.getByLabelText("开始日期") as HTMLInputElement).value).toBe("")
    expect((screen.getByLabelText("结束日期") as HTMLInputElement).value).toBe("")
    expect(screen.queryByRole("link", { name: "筛选会话" })).toBeNull()
  })

  it("rejects reversed dates without sending requests and accepts an open-ended range", async () => {
    mount()
    await screen.findByRole("link", { name: "会话 1" })
    vi.mocked(http.get).mockClear()
    vi.mocked(http.get).mockImplementation(async (path) =>
      path.startsWith("/api/billing/summary")
        ? { total_tokens: 0, total_credits: "0", historical_count: 0, unpriced_count: 0 }
        : { items: [], total: 0, total_pages: 1, page: 1, page_size: 20 },
    )
    fireEvent.change(screen.getByLabelText("开始日期"), { target: { value: "2026-09-05" } })
    fireEvent.change(screen.getByLabelText("结束日期"), { target: { value: "2026-09-01" } })
    fireEvent.click(screen.getByRole("button", { name: "筛选" }))
    expect(screen.getByRole("alert").textContent).toBe("开始日期不能晚于结束日期")
    expect(http.get).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText("开始日期"), { target: { value: "" } })
    fireEvent.click(screen.getByRole("button", { name: "筛选" }))
    await waitFor(() => expect(http.get).toHaveBeenCalledTimes(2))
    for (const [path] of vi.mocked(http.get).mock.calls) {
      const params = new URL(path, "http://test").searchParams
      expect(params.has("date_from")).toBe(false)
      expect(params.get("date_to")).toBe("2026-09-01")
    }
    expect(screen.queryByRole("alert")).toBeNull()
    await screen.findByText("所选日期内没有消耗记录")
  })

  it("offers retry on a failed usage request without claiming the history is empty", async () => {
    vi.mocked(http.get).mockRejectedValue(new Error("offline"))
    mount()
    await screen.findByText("积分与用量加载失败")
    expect(screen.queryByText("还没有模型消耗记录")).toBeNull()
    expect(screen.getAllByRole("button", { name: "重试" }).length).toBeGreaterThan(0)
  })

  it("reuses the request key after an uncertain payment response", async () => {
    vi.mocked(http.get).mockResolvedValue({ items: [{ id: "gateway", name: "测试渠道" }] })
    vi.mocked(http.post).mockRejectedValue(new Error("timeout"))
    mount(
      <CreditTopUp
        rules={{
          min_amount_fen: 100,
          max_amount_fen: 10000000,
          presets_fen: [1000, 5000, 10000, 50000],
          credits_per_yuan: "1",
        }}
        allowed
        canManage
      />,
    )
    const submit = await screen.findByRole("button", { name: "充值" })
    fireEvent.click(submit)
    await screen.findByRole("alert")
    fireEvent.click(submit)
    await waitFor(() => expect(http.post).toHaveBeenCalledTimes(2))
    const bodies = vi.mocked(http.post).mock.calls.map((call) => call[1])
    expect(bodies[1]).toEqual(bodies[0])
    expect(bodies[0]).toMatchObject({ provider: "gateway", amount_fen: 1000 })
  })

  it("recovers an existing order after remount without creating another order", async () => {
    const pending = {
      id: "pay-existing",
      kind: "topup",
      amount_fen: 1000,
      provider: "gateway",
      credits: "10",
      status: "pending",
      checkout_url: null,
      created_at: "2026-09-05T01:00:00Z",
      paid_at: null,
    }
    let order = { ...pending, checkout_url: null as string | null }
    vi.mocked(http.get).mockImplementation(async (path) =>
      path.endsWith("/providers")
        ? { items: [{ id: "gateway", name: "测试支付渠道" }] }
        : {
            items: [order],
            page: 1,
            total: 1,
            total_pages: 1,
          },
    )
    vi.mocked(http.post).mockImplementation(async () => {
      order = { ...order, checkout_url: "https://checkout.example.test/existing" }
      return order
    })
    const first = mount(<PaymentHistory />)
    await screen.findByRole("button", { name: "继续支付" })
    first.unmount()
    mount(<PaymentHistory />)
    fireEvent.click(await screen.findByRole("button", { name: "继续支付" }))
    await waitFor(() => expect(redirect).toHaveBeenCalled())
    expect(redirect.mock.calls[0][0]).toMatchObject({
      id: "pay-existing",
      checkout_url: "https://checkout.example.test/existing",
    })
    expect(http.post).toHaveBeenCalledExactlyOnceWith(
      "/api/billing/orders/pay-existing/checkout",
      undefined,
      { headers: { "X-Workspace-Id": "workspace" } },
    )
    expect(within(screen.getByRole("listitem")).getByText("待支付")).toBeDefined()
  })

  it("paginates payment history and refreshes balance after a paid order is observed", async () => {
    let credited = false
    vi.mocked(http.get).mockImplementation(async (path) => {
      if (path.endsWith("/balance")) return { balance: credited ? "10" : "0" }
      if (path.endsWith("/providers")) return { items: [] }
      if (path.startsWith("/api/billing/orders?")) {
        const page = new URL(path, "http://test").searchParams.get("page")
        if (page === "2") credited = true
        return {
          items: [
            {
              id: "pay-" + page,
              kind: "topup",
              amount_fen: 1000,
              provider: "gateway",
              credits: "10",
              status: page === "2" ? "paid" : "pending",
              checkout_url: "https://checkout.example.test/one",
              created_at: "2026-09-05T01:00:00Z",
            },
          ],
          page: Number(page),
          total: 11,
          total_pages: 2,
        }
      }
      return {}
    })
    mount(
      <>
        <PaymentHistory />
        <BalanceProbe />
      </>,
    )
    await screen.findByText("订单号：pay-1")
    await screen.findByText("余额：0")
    const pagination = within(screen.getByRole("navigation", { name: "订单分页" }))
    fireEvent.click(pagination.getByRole("button", { name: "下一页" }))
    await screen.findByText("订单号：pay-2")
    expect(within(screen.getByRole("listitem")).getByText("已支付")).toBeDefined()
    expect(screen.queryByRole("link")).toBeNull()
    await screen.findByText("余额：10")
  })

  it("clears the displayed checkout and pagination when switching workspace", async () => {
    mount()
    await screen.findByRole("link", { name: "会话 1" })
    fireEvent.click(screen.getByRole("button", { name: "下一页" }))
    await screen.findByRole("link", { name: "会话 2" })
    act(() => useWorkspaceStore.setState({ currentId: "second-workspace" }))
    await waitFor(() =>
      expect(http.get).toHaveBeenCalledWith("/api/billing/usage?page=1&page_size=20", {
        headers: { "X-Workspace-Id": "second-workspace" },
      }),
    )
    expect(screen.queryByRole("link", { name: "会话 2" })).toBeNull()
  })
})


describe("media usage rows", () => {
  it("shows duration and billed minutes instead of tokens for a video composition", async () => {
    vi.mocked(http.get).mockImplementation(async (path) => {
      if (path.endsWith("/balance")) return { balance: "8.25", mode: "shadow" }
      if (path.endsWith("/summary"))
        return { total_tokens: 0, total_credits: "0.03", historical_count: 0, unpriced_count: 0 }
      if (path.endsWith("/providers")) return { items: [] }
      if (path.startsWith("/api/billing/orders?")) return { items: [], page: 1, total: 0, total_pages: 1 }
      return {
        total: 1, total_pages: 1, page: 1, page_size: 20,
        items: [{
          id: "e1", session_id: "s1", session_title: "口播成片", session_available: true,
          model_id: "ims-compose-720p", kind: "video_compose",
          tokens: { duration_sec: 14.4, minutes_billed: 1, tier: "720p" },
          total_tokens: 0, credits: "0.030000000000", status: "shadow",
          created_at: "2026-09-09T10:58:17Z", pricing_version: "2026-09-05.2",
        }],
      }
    })
    mount()
    await waitFor(() => expect(screen.getByText("口播成片")).toBeDefined())
    expect(screen.getByText("时长 14.4 秒")).toBeDefined()
    expect(screen.getByText("计费 1 分钟")).toBeDefined()
    expect(screen.getByText("档位 720p")).toBeDefined()
    expect(screen.getByText(/视频合成 · 已统计/)).toBeDefined()
    expect(screen.queryByText(/输入/)).toBeNull()
  })
})

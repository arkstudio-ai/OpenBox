import { Suspense } from "react"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import { MemoryRouter } from "react-router"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import type { PricingItem, PricingTable } from "../types"
import { PricingPage } from "./PricingPage"

vi.mock("@/shared/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/http")>()
  return { ...actual, http: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }
})

const clients: QueryClient[] = []

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  clients.push(client)
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={["/app/admin/pricing"]}>
          <Suspense fallback={null}>
            <PricingPage />
          </Suspense>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

function item(overrides: Partial<PricingItem>): PricingItem {
  return {
    key: "video-gen:MiniMax-H3:768p",
    kind: "video-gen",
    model: "MiniMax-H3",
    resolution: "768p",
    label: "MiniMax H3 768p",
    unit: "per_second",
    alias_of: null,
    channel: null,
    base_sale: { per_second: "0.50" },
    sale: { per_second: "0.50" },
    cost: { per_second: "0.09", basis: "metaso", channel: "ch114 → metaso", currency: "CNY" },
    sale_credits: { per_second: "0.5" },
    cost_credits: { per_second: "0.09" },
    margin_pct: { per_second: "455.6" },
    below_cost_fields: [],
    flags: ["configured"],
    expires_at: null,
    rule: null,
    usage_30d: {
      events: 184,
      charged_events: 163,
      credits: "803",
      shadow_credits: "76.5",
      cost_credits: "72.27",
      costed_events: 163,
      tokens: 0,
      unpriced_events: 0,
      gross_margin: "730.73",
    },
    ...overrides,
  }
}

function table(items: PricingItem[]): PricingTable {
  return {
    as_of: "2026-10-10T12:00:00Z",
    window_days: 30,
    catalogue_version: "2026-10-10.1",
    base_version: "2026-10-10.1",
    usd_cny: "6.7787",
    rules_loaded_at: "2026-10-10T11:59:00Z",
    effective_within_seconds: 30,
    summary: {
      credits: "3800",
      cost_credits: "900",
      gross_margin: "2900",
      costed_events: 200,
      charged_events: 300,
      flags: { below_cost: 1, unpriced: 0, expiring: 1, overridden: 0, no_cost: 2 },
    },
    items,
  }
}

const gemini = item({
  key: "llm:gemini-3.8-flash",
  kind: "llm",
  model: "gemini-3.8-flash",
  resolution: null,
  label: "Gemini 3.8 Flash",
  unit: "per_million",
  alias_of: "gemini-3.7-flash",
  base_sale: { input: "0.75", output: "3.75", cache_read: "0.075", currency: "USD" },
  sale: { input: "0.75", output: "3.75", cache_read: "0.075", currency: "USD" },
  cost: { input: "6", output: "30", cache_read: "0.7", basis: "rovinai", currency: "CNY" },
  sale_credits: { input: "5.084025", output: "25.420125", cache_read: "0.5084025" },
  cost_credits: { input: "6", output: "30", cache_read: "0.7" },
  margin_pct: { input: "-15.3", output: "-15.3", cache_read: "-27.4" },
  below_cost_fields: ["input", "output", "cache_read"],
  flags: ["below_cost", "alias", "expiring", "configured"],
  expires_at: "2027-01-01T00:00:00Z",
})

describe("PricingPage", () => {
  beforeEach(async () => {
    useAuthStore.setState({ user: { id: "admin-1", username: "ops", role: "admin" } } as never)
    await i18n.changeLanguage("zh-CN")
    await i18n.loadNamespaces("admin-pricing")
    vi.mocked(http.get).mockReset()
    vi.mocked(http.put).mockReset()
    vi.mocked(http.post).mockReset()
  })
  afterEach(() => {
    cleanup()
    clients.splice(0).forEach((client) => client.clear())
  })

  it("lists every item with cost, sale price, margin and flags, and filters by flag", async () => {
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      if (path === "/api/admin/pricing") return table([item({}), gemini])
      return { key: path, revisions: [], operations: [] }
    })
    mount()
    const rows = await screen.findAllByRole("row")
    expect(rows.length).toBe(3) // header + 2
    const h3 = rows[1]
    expect(within(h3).getByText("MiniMax H3 768p")).toBeTruthy()
    expect(within(h3).getByText("+455.6%")).toBeTruthy()
    expect(within(h3).getByText("口径：metaso")).toBeTruthy()
    const row2 = rows[2]
    expect(within(row2).getByText("低于成本")).toBeTruthy()
    expect(within(row2).getByText("跟随 gemini-3.7-flash 的价格")).toBeTruthy()
    // Summary tiles.
    expect(screen.getAllByText("毛利")[0].nextElementSibling?.textContent).toContain("2,900")
    // Flag filter narrows to the below-cost row only.
    fireEvent.change(screen.getByLabelText("状态"), { target: { value: "below_cost" } })
    await waitFor(() => expect(screen.getAllByRole("row").length).toBe(2))
    expect(screen.queryByText("MiniMax H3 768p")).toBeNull()
  })

  it("opens the editor prefilled, refuses a blank reason, and sends the write with the revision it saw", async () => {
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      if (path === "/api/admin/pricing") return table([item({})])
      return { key: path, revisions: [], operations: [] }
    })
    vi.mocked(http.put).mockResolvedValue({
      key: "video-gen:MiniMax-H3:768p",
      rule: { id: "pricing_1", revision: 1 },
      effective_within_seconds: 30,
    })
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "改价" }))
    const dialog = await screen.findByRole("dialog")
    const perSecond = within(dialog).getByLabelText(/每秒/) as HTMLInputElement
    expect(perSecond.value).toBe("0.50")
    const save = within(dialog).getByRole("button", { name: "保存售价" }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(perSecond, { target: { value: "0.40" } })
    fireEvent.change(within(dialog).getByLabelText("修改原因（必填）"), { target: { value: "跟进秘塔降价" } })
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() => expect(http.put).toHaveBeenCalledTimes(1))
    const [path, body] = vi.mocked(http.put).mock.calls[0] as [string, Record<string, unknown>]
    expect(path).toBe("/api/admin/pricing/video-gen%3AMiniMax-H3%3A768p")
    expect(body).toMatchObject({
      reason: "跟进秘塔降价",
      expected_revision: 0,
      status: "active",
      sale: { per_second: "0.40" },
      cost: null,
      allow_below_cost: false,
      confirm_disable: false,
    })
    expect(String(body.request_key)).toMatch(/^[0-9a-f]{32}$/)
  })

  it("explains a below-cost refusal from the server", async () => {
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      if (path === "/api/admin/pricing") return table([item({})])
      return { key: path, revisions: [], operations: [] }
    })
    const { ApiError } = await import("@/shared/api/http")
    vi.mocked(http.put).mockRejectedValue(new ApiError(422, "BELOW_COST", "below", { fields: ["per_second"] }))
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "改价" }))
    const dialog = await screen.findByRole("dialog")
    fireEvent.change(within(dialog).getByLabelText(/每秒/), { target: { value: "0.01" } })
    fireEvent.change(within(dialog).getByLabelText("修改原因（必填）"), { target: { value: "x" } })
    fireEvent.click(within(dialog).getByRole("button", { name: "保存售价" }))
    const alert = await within(dialog).findByRole("alert")
    expect(alert.textContent).toContain("低于成本（per_second）")
  })
})

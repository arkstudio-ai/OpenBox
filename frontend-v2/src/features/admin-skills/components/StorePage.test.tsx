import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, useLocation } from "react-router"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { http } from "@/shared/api/http"
import { StorePage } from "./StorePage"

vi.mock("@/shared/api/http", () => ({
  http: { get: vi.fn(), post: vi.fn() },
  requestBlob: vi.fn(),
}))

const page = {
  total: 2,
  offset: 0,
  limit: 20,
  items: [
    {
      catalog_id: "community:7",
      kind: "skill",
      origin: "community",
      title: "网页调研",
      name: "web-research",
      icon: "🔎",
      author: { username: "lin", email: "lin@example.com" },
      version: 3,
      published_at: "2026-09-01T02:00:00Z",
      installs_count: 12,
      listing: "listed",
      listing_note: null,
      featured: false,
      is_official: false,
    },
    {
      catalog_id: "skill:anthropic-skills",
      kind: "skill",
      origin: "third_party",
      title: "Anthropic 技能包",
      name: "anthropic-skills",
      publisher: "Anthropic",
      version: null,
      published_at: null,
      installs_count: 0,
      listing: "delisted",
      listing_note: "默认下架",
      featured: false,
      is_official: false,
    },
  ],
}

function Probe() {
  const location = useLocation()
  return <output data-testid="url">{`${location.pathname}${location.search}`}</output>
}

function mount(entry = "/app/admin/skills/store") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <StorePage />
          <Probe />
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

const url = () => screen.getByTestId("url").textContent
const paths = () => vi.mocked(http.get).mock.calls.map(([path]) => path)

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("admin-skills")
})

beforeEach(() => {
  vi.mocked(http.get).mockReset().mockResolvedValue(page)
  vi.mocked(http.post).mockReset().mockResolvedValue({})
})

afterEach(cleanup)

describe("StorePage", () => {
  it("starts unfiltered and puts every filter into the URL", async () => {
    mount()
    await screen.findByText("网页调研")
    expect(paths()[0]).toBe("/api/admin/skills/store?offset=0&limit=20")

    fireEvent.click(screen.getByRole("button", { name: "官方" }))
    await waitFor(() => expect(url()).toBe("/app/admin/skills/store?origin=official"))
    await waitFor(() =>
      expect(paths()).toContain("/api/admin/skills/store?origin=official&offset=0&limit=20"),
    )

    // "全部" is the default, so it leaves the address clean again.
    fireEvent.click(within(screen.getByRole("group", { name: "来源" })).getByRole("button", { name: "全部" }))
    await waitFor(() => expect(url()).toBe("/app/admin/skills/store"))

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索商店条目" }), {
      target: { value: "调研" },
    })
    await waitFor(() => expect(url()).toBe("/app/admin/skills/store?q=%E8%B0%83%E7%A0%94"))
  })

  it("sends a filter change back to the first page", async () => {
    mount("/app/admin/skills/store?offset=20")
    await screen.findByText("网页调研")
    fireEvent.click(screen.getByRole("button", { name: "MCP" }))
    await waitFor(() => expect(url()).toBe("/app/admin/skills/store?kind=mcp"))
  })

  it("refuses to delist without a reason, then posts the one it was given", async () => {
    mount()
    // The listing filter also has a 下架 pill, so reach for the row's own button.
    const row = (await screen.findByText("网页调研")).closest("tr") as HTMLElement
    fireEvent.click(within(row).getByRole("button", { name: "下架" }))

    const dialog = screen.getByRole("dialog")
    const confirm = within(dialog).getByRole("button", { name: "下架" })
    expect(confirm).toHaveProperty("disabled", true)
    fireEvent.click(confirm)
    expect(http.post).not.toHaveBeenCalled()

    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "与官方技能重复" } })
    fireEvent.click(within(dialog).getByRole("button", { name: "下架" }))
    await waitFor(() =>
      // The colon in a catalog id has to survive as %3A.
      expect(http.post).toHaveBeenCalledWith("/api/admin/skills/store/community%3A7/listing", {
        listing: "delisted",
        note: "与官方技能重复",
      }),
    )
  })

  it("hands the installs count over to the installs tab, pre-filtered", async () => {
    mount()
    await screen.findByText("网页调研")
    fireEvent.click(screen.getByRole("button", { name: "12" }))
    expect(url()).toBe("/app/admin/skills/installs?catalog=community%3A7")
  })

  it("offers 标为官方 only on community rows", async () => {
    mount()
    await screen.findByText("网页调研")
    expect(screen.getAllByRole("button", { name: "标为官方" }).length).toBe(1)
    expect(screen.getAllByRole("button", { name: "查看" }).length).toBe(1)
  })
})

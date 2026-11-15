import { Suspense } from "react"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import { MemoryRouter } from "react-router"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import { ExamplesPage } from "./ExamplesPage"
import type { Coverage, Example, ExampleList, Taxonomy } from "./types"

vi.mock("@/shared/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/http")>()
  return { ...actual, http: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }
})

const TAXONOMY: Taxonomy = {
  version: "t",
  entries: [
    { id: "video", label: "做一条视频" },
    { id: "menu", label: "做菜单" },
    {
      id: "poster",
      label: "做海报",
      scenes: [
        { id: "new_item", label: "新品上市" },
        { id: "holiday", label: "节日活动" },
      ],
    },
  ],
  industries: [
    {
      id: "food",
      label: "餐饮",
      shop_types: [
        { id: "noodle", label: "面馆 / 粉店", keywords: [] },
        { id: "other", label: "其他餐饮", keywords: [] },
      ],
    },
    { id: "beauty", label: "美业", shop_types: [{ id: "nail", label: "美甲", keywords: [] }, { id: "other", label: "其他", keywords: [] }] },
  ],
}

function example(overrides: Partial<Example>): Example {
  return {
    id: "lex_1",
    entry_id: "video",
    industry: "food",
    shop_type: "noodle",
    subtype: null,
    scene: null,
    scene_label: "",
    tags: ["探店"],
    rating: 5,
    status: "active",
    title: "面馆探店样例",
    notes: "先门头再上面",
    recipe: "画面：牛肉面特写",
    inputs: [],
    outputs: [{ asset_id: "asset_final", oss_key: "assets/u/asset_final/final.mp4", name: "final.mp4", mime: "video/mp4" }],
    source: { session_id: "session_1", shop_name: "巷口面馆" },
    created_by: "admin-1",
    created_at: "2026-11-04T00:00:00Z",
    updated_at: "2026-11-04T00:00:00Z",
    use_count: 7,
    pick_count: 0,
    industry_label: "餐饮",
    shop_type_label: "餐饮 / 面馆 / 粉店",
    ...overrides,
  }
}

const list = (items: Example[]): ExampleList => ({ items, total: items.length, limit: 50, offset: 0 })
const clients: QueryClient[] = []

function mount(entry = "/app/admin/examples") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  clients.push(client)
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <Suspense fallback={null}>
            <ExamplesPage />
          </Suspense>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

describe("ExamplesPage", () => {
  beforeEach(async () => {
    useAuthStore.setState({ user: { id: "admin-1", username: "ops", role: "admin" } } as never)
    await i18n.changeLanguage("zh-CN")
    await i18n.loadNamespaces(["admin-examples", "common"])
    vi.mocked(http.get).mockReset()
    vi.mocked(http.post).mockReset()
    vi.mocked(http.put).mockReset()
  })
  afterEach(() => {
    cleanup()
    clients.splice(0).forEach((client) => client.clear())
  })

  it("lists examples with their classification, rating and use count, and filters server-side", async () => {
    const calls: string[] = []
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      calls.push(path)
      if (path.endsWith("/taxonomy")) return TAXONOMY
      if (path.includes("/assets/")) return { url: "https://signed/final.mp4", mime: "video/mp4", name: "final.mp4", expires_in: 3600 }
      if (path.includes("industry=beauty")) return list([])
      return list([example({}), example({ id: "lex_2", title: "停用的", status: "disabled", entry_id: "menu", use_count: 0 })])
    })
    mount()
    const rows = await screen.findAllByRole("row")
    expect(rows.length).toBe(3)
    expect(within(rows[1]!).getByText("面馆探店样例")).toBeTruthy()
    expect(within(rows[1]!).getByText("餐饮 / 面馆 / 粉店")).toBeTruthy()
    expect(within(rows[1]!).getByText("5/5")).toBeTruthy()
    expect(within(rows[1]!).getByText("7")).toBeTruthy()
    expect(within(rows[1]!).getByText("探店")).toBeTruthy()
    expect(within(rows[2]!).getByText("停用")).toBeTruthy()
    expect(within(rows[2]!).getByText("做菜单")).toBeTruthy()
    expect(calls.find((path) => path.startsWith("/api/admin/examples?"))).toBe("/api/admin/examples?limit=50")

    fireEvent.change(screen.getByLabelText("行业"), { target: { value: "beauty" } })
    await waitFor(() => expect(calls.some((path) => path.includes("industry=beauty"))).toBe(true))
    expect(await screen.findByText(/还没有样例/)).toBeTruthy()
    // The shop-type filter lists only that industry's types.
    const shopTypes = screen.getByLabelText("门店类型") as HTMLSelectElement
    expect(Array.from(shopTypes.options).map((option) => option.textContent)).toEqual(["全部", "美甲", "其他"])
    fireEvent.change(shopTypes, { target: { value: "nail" } })
    await waitFor(() => expect(calls.some((path) => path.includes("industry=beauty&shop_type=nail"))).toBe(true))
    fireEvent.change(screen.getByLabelText("状态"), { target: { value: "disabled" } })
    await waitFor(() => expect(calls.some((path) => path.includes("status=disabled"))).toBe(true))
  })

  it("shows the scene and the pick count, and filters by the entry's scenes or by none", async () => {
    const calls: string[] = []
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      calls.push(path)
      if (path.endsWith("/taxonomy")) return TAXONOMY
      if (path.endsWith("/coverage")) return { target: 3, entries: [] }
      return list([example({ entry_id: "poster", scene: "holiday", scene_label: "节日活动", pick_count: 12 })])
    })
    mount()
    const rows = await screen.findAllByRole("row")
    expect(within(rows[1]!).getByText("做海报")).toBeTruthy()
    expect(within(rows[1]!).getByText("节日活动")).toBeTruthy()
    expect(within(rows[1]!).getByText("12")).toBeTruthy()

    const scene = screen.getByLabelText("类型") as HTMLSelectElement
    const labels = () => Array.from(scene.options).map((option) => option.textContent)
    expect(labels()).toEqual(["全部", "未分类"])
    fireEvent.change(scene, { target: { value: "none" } })
    await waitFor(() => expect(calls.some((path) => path.includes("scene=none"))).toBe(true))
    // Unclassified reads the same under any entry, so it survives the entry change; a named scene does not.
    fireEvent.change(screen.getByLabelText("入口"), { target: { value: "poster" } })
    await waitFor(() => expect(calls.some((path) => path.includes("entry_id=poster&scene=none"))).toBe(true))
    expect(labels()).toEqual(["全部", "新品上市", "节日活动", "未分类"])
    fireEvent.change(scene, { target: { value: "holiday" } })
    await waitFor(() => expect(calls.some((path) => path.includes("entry_id=poster&scene=holiday"))).toBe(true))
    fireEvent.change(screen.getByLabelText("入口"), { target: { value: "menu" } })
    await waitFor(() => expect(calls.some((path) => path === "/api/admin/examples?entry_id=menu&limit=50")).toBe(true))
  })

  it("counts active examples per entry and scene and flags the cells under the target", async () => {
    const calls: string[] = []
    const coverage: Coverage = {
      target: 3,
      entries: [
        { entry_id: "video", label: "做一条视频", scenes: [], unclassified: 4, total: 4 },
        {
          entry_id: "poster",
          label: "做海报",
          scenes: [
            { scene: "new_item", label: "新品上市", count: 3 },
            { scene: "holiday", label: "节日活动", count: 1 },
          ],
          unclassified: 2,
          total: 6,
        },
      ],
    }
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      calls.push(path)
      if (path.endsWith("/taxonomy")) return TAXONOMY
      if (path.endsWith("/coverage")) return coverage
      return list([])
    })
    mount()
    const section = await screen.findByTestId("example-coverage")
    // Only entries that are split by scene get a row.
    expect(within(section).getAllByTestId("coverage-row").length).toBe(1)
    const cells = within(section).getAllByTestId("coverage-cell")
    expect(cells.map((cell) => [cell.textContent, cell.dataset.under])).toEqual([
      ["新品上市 3/3", "false"],
      ["节日活动 1/3", "true"],
    ])
    expect(cells[1]!.className).toContain("text-dangerink")
    expect(cells[0]!.className).not.toContain("text-dangerink")
    expect(cells[1]!.getAttribute("title")).toBe("还差 2 条")
    expect(within(section).getByTestId("coverage-unclassified").textContent).toBe("未分类 2")

    // A cell is a shortcut to that slice of the list.
    fireEvent.click(cells[1]!)
    await waitFor(() => expect(calls.some((path) => path.includes("entry_id=poster&scene=holiday"))).toBe(true))
    expect((screen.getByLabelText("类型") as HTMLSelectElement).value).toBe("holiday")
    fireEvent.click(within(section).getByTestId("coverage-unclassified"))
    await waitFor(() => expect(calls.some((path) => path.includes("entry_id=poster&scene=none"))).toBe(true))
  })

  it("previews an output through the admin link and toggles status", async () => {
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/taxonomy")) return TAXONOMY
      if (path.includes("/assets/asset_final/url")) return { url: "https://signed/final.mp4", mime: "video/mp4", name: "final.mp4", expires_in: 3600 }
      return list([example({})])
    })
    vi.mocked(http.post).mockResolvedValue(example({ status: "disabled" }))
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "预览" }))
    const preview = await screen.findByTestId("example-preview")
    expect(within(preview).getByText("画面：牛肉面特写")).toBeTruthy()
    await waitFor(() => expect(preview.querySelector("video")?.getAttribute("src")).toBe("https://signed/final.mp4"))
    expect(vi.mocked(http.get).mock.calls.some(([path]) => path === "/api/admin/examples/assets/asset_final/url")).toBe(true)

    fireEvent.click(screen.getByRole("button", { name: "停用" }))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/admin/examples/lex_1/status", { status: "disabled" }))
  })

  it("opens the edit sheet prefilled and sends the full row back", async () => {
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/taxonomy")) return TAXONOMY
      if (path.includes("/assets/")) return { url: "https://signed/final.mp4", mime: "video/mp4", name: "final.mp4", expires_in: 3600 }
      return list([example({})])
    })
    vi.mocked(http.put).mockResolvedValue(example({ rating: 4 }))
    mount()
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }))
    const dialog = await screen.findByRole("dialog")
    expect((within(dialog).getByLabelText("标题") as HTMLInputElement).value).toBe("面馆探店样例")
    expect((within(dialog).getByLabelText("标签") as HTMLInputElement).value).toBe("探店")
    fireEvent.change(within(dialog).getByLabelText("评分"), { target: { value: "4" } })
    fireEvent.change(within(dialog).getByLabelText("标签"), { target: { value: "探店, 傍晚, 探店" } })
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }))
    await waitFor(() => expect(http.put).toHaveBeenCalledTimes(1))
    const [path, body] = vi.mocked(http.put).mock.calls[0] as [string, Record<string, unknown>]
    expect(path).toBe("/api/admin/examples/lex_1")
    expect(body).toMatchObject({
      entry_id: "video",
      industry: "food",
      shop_type: "noodle",
      rating: 4,
      tags: ["探店", "傍晚"],
      status: "active",
      outputs: [{ asset_id: "asset_final" }],
      source: { session_id: "session_1", shop_name: "巷口面馆" },
    })
  })
})

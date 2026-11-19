import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError, http } from "@/shared/api/http"
import { TaxonomySheet } from "./TaxonomySheet"
import type { Taxonomy } from "./types"

vi.mock("@/shared/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/http")>()
  return { ...actual, http: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }
})

const TAXONOMY: Taxonomy = {
  version: "t",
  entries: [
    { id: "video", label: "做一条视频", scenes: [] },
    { id: "poster", label: "做海报", scenes: [{ id: "new_item", label: "新品上市" }, { id: "c_1", label: "会员日" }] },
  ],
  industries: [
    { id: "food", label: "餐饮", shop_types: [{ id: "noodle", label: "面馆", keywords: ["面", "粉"] }, { id: "other", label: "其他餐饮", keywords: [] }] },
    { id: "c_2", label: "教培", shop_types: [{ id: "other", label: "其他", keywords: [] }] },
  ],
  builtin: { scenes: { video: [], poster: ["new_item"] }, industries: ["food"], shop_types: ["other"] },
}

const clients: QueryClient[] = []

function mount(onSaved = vi.fn(), onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  clients.push(client)
  render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <TaxonomySheet open onClose={onClose} onSaved={onSaved} />
      </I18nextProvider>
    </QueryClientProvider>,
  )
  return { onSaved, onClose }
}

describe("TaxonomySheet", () => {
  beforeEach(async () => {
    useAuthStore.setState({ user: { id: "admin-1", username: "ops", role: "admin" } } as never)
    await i18n.changeLanguage("zh-CN")
    await i18n.loadNamespaces(["admin-examples", "common"])
    vi.mocked(http.get).mockReset().mockResolvedValue(TAXONOMY)
    vi.mocked(http.put).mockReset()
  })
  afterEach(() => {
    cleanup()
    clients.splice(0).forEach((client) => client.clear())
  })

  it("adds, renames and removes classes, and keeps the built-in ones", async () => {
    vi.mocked(http.put).mockResolvedValue(TAXONOMY)
    const { onSaved, onClose } = mount()
    const [, poster] = await screen.findAllByTestId("taxonomy-entry")
    // Built in: renamed, not removed. Added by the team: removable.
    expect((within(poster!).getByRole("button", { name: "移除 新品上市" }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(within(poster!).getByRole("button", { name: "移除 会员日" }))
    fireEvent.click(within(poster!).getByRole("button", { name: "+ 加类型" }))
    const save = screen.getByRole("button", { name: "保存" }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(screen.getByText("有名称还没填")).toBeTruthy()
    const names = within(poster!).getAllByLabelText("做海报的类型名称")
    fireEvent.change(names[1]!, { target: { value: "新品上市" } })
    expect(screen.getByText("同一组里有重名")).toBeTruthy()
    fireEvent.change(names[1]!, { target: { value: "周年庆" } })

    const [food, school] = screen.getAllByTestId("taxonomy-industry")
    expect((within(food!).getByRole("button", { name: "移除 餐饮" }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(food!).getByRole("button", { name: "移除 其他餐饮" }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(within(food!).getByRole("button", { name: "+ 加门店类型" }))
    const shops = within(food!).getAllByLabelText("餐饮的门店类型名称") as HTMLInputElement[]
    expect(shops.map((input) => input.value)).toEqual(["面馆", "", "其他餐饮"])
    fireEvent.change(shops[1]!, { target: { value: "茶餐厅" } })
    fireEvent.change(within(food!).getByLabelText("茶餐厅的关键词"), { target: { value: "茶餐厅、冰室，茶餐厅" } })
    fireEvent.click(within(school!).getByRole("button", { name: "移除 教培" }))
    fireEvent.click(screen.getByRole("button", { name: "+ 加行业" }))
    fireEvent.change(screen.getAllByLabelText("行业名称")[1]!, { target: { value: "宠物" } })

    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(onClose).toHaveBeenCalled()
    expect(vi.mocked(http.put).mock.calls[0]).toEqual([
      "/api/admin/examples/taxonomy",
      {
        scenes: { video: [], poster: [{ id: "new_item", label: "新品上市" }, { label: "周年庆" }] },
        industries: [
          {
            id: "food",
            label: "餐饮",
            shop_types: [
              { id: "noodle", label: "面馆", keywords: ["面", "粉"] },
              { label: "茶餐厅", keywords: ["茶餐厅", "冰室"] },
              { id: "other", label: "其他餐饮", keywords: [] },
            ],
          },
          { label: "宠物", shop_types: [{ id: "other", label: "其他", keywords: [] }] },
        ],
      },
    ])
  })

  it("says which examples still use a class the server would not let go", async () => {
    vi.mocked(http.put).mockRejectedValue(new ApiError(409, "TAXONOMY_IN_USE", "做海报 / 会员日（2 条）"))
    const { onClose } = mount()
    const [, poster] = await screen.findAllByTestId("taxonomy-entry")
    fireEvent.click(within(poster!).getByRole("button", { name: "移除 会员日" }))
    fireEvent.click(screen.getByRole("button", { name: "保存" }))
    expect((await screen.findByRole("alert")).textContent).toContain("做海报 / 会员日（2 条）")
    expect(onClose).not.toHaveBeenCalled()
  })
})

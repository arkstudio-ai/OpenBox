// The collect flow: the page is opened with ?collect=<session> from the
// trajectory turn page, the sheet reads the session, shows what came back,
// and the team adds only shop type, tags, rating and notes.
import { Suspense } from "react"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import { MemoryRouter, Route, Routes, useLocation } from "react-router"
import i18n from "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError, http } from "@/shared/api/http"
import { ExamplesPage } from "./ExamplesPage"
import { splitTags, toWrite, EMPTY } from "./lib"
import type { ExampleDraft, Taxonomy } from "./types"

vi.mock("@/shared/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/http")>()
  return { ...actual, http: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }
})

const TAXONOMY: Taxonomy = {
  version: "t",
  entries: [
    { id: "video", label: "做一条视频" },
    { id: "poster", label: "做海报" },
  ],
  industries: [
    {
      id: "food",
      label: "餐饮",
      shop_types: [
        { id: "noodle", label: "面馆 / 粉店", keywords: [] },
        { id: "hotpot", label: "火锅", keywords: [] },
        { id: "other", label: "其他餐饮", keywords: [] },
      ],
    },
    { id: "beauty", label: "美业", shop_types: [{ id: "nail", label: "美甲", keywords: [] }, { id: "other", label: "其他", keywords: [] }] },
  ],
}

const DRAFT: ExampleDraft = {
  entry_id: "video",
  industry: "food",
  shop_type: "noodle",
  subtype: null,
  title: "做一条视频",
  recipe: "方案卡：\n画面：牛肉面特写\n\n生成提示词：\n竖屏美食短片",
  inputs: [{ asset_id: "asset_photo", oss_key: "assets/u/asset_photo/photo.jpg", name: "photo.jpg", mime: "image/jpeg" }],
  outputs: [{ asset_id: "asset_final", oss_key: "assets/u/asset_final/final.mp4", name: "final.mp4", mime: "video/mp4" }],
  source: { session_id: "session_1", job_id: "job_1", user_id: "u1", workspace_id: "w1", shop_name: "巷口面馆", headline: "红烧牛肉面" },
}

const clients: QueryClient[] = []
let location = ""

function Probe() {
  const here = useLocation()
  location = here.pathname + here.search
  return null
}

function mount(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  clients.push(client)
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <Suspense fallback={null}>
            <Routes>
              <Route
                path="/app/admin/examples"
                element={
                  <>
                    <Probe />
                    <ExamplesPage />
                  </>
                }
              />
            </Routes>
          </Suspense>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

describe("collect sheet", () => {
  beforeEach(async () => {
    useAuthStore.setState({ user: { id: "admin-1", username: "ops", role: "admin" } } as never)
    await i18n.changeLanguage("zh-CN")
    await i18n.loadNamespaces(["admin-examples", "common"])
    vi.mocked(http.get).mockReset()
    vi.mocked(http.post).mockReset()
    vi.mocked(http.put).mockReset()
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/taxonomy")) return TAXONOMY
      if (path.includes("/assets/asset_final/")) return { url: "https://signed/final.mp4", mime: "video/mp4", name: "final.mp4", expires_in: 3600 }
      if (path.includes("/assets/asset_photo/")) return { url: "https://signed/photo.jpg", mime: "image/jpeg", name: "photo.jpg", expires_in: 3600 }
      return { items: [], total: 0, limit: 50, offset: 0 }
    })
  })
  afterEach(() => {
    cleanup()
    clients.splice(0).forEach((client) => client.clear())
  })

  it("reads the session named in the URL, shows what came back, and saves with the team's additions", async () => {
    vi.mocked(http.post).mockImplementation(async (path: string) => {
      if (path.endsWith("/prefill")) return DRAFT
      return { ...DRAFT, id: "lex_new", status: "active" }
    })
    mount("/app/admin/examples?collect=session_1&asset=asset_final")
    const dialog = await screen.findByRole("dialog")
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/admin/examples/prefill", { session_id: "session_1", asset_id: "asset_final" }))
    const title = (await within(dialog).findByLabelText("标题")) as HTMLInputElement
    expect(title.value).toBe("做一条视频")
    expect((within(dialog).getByLabelText("入口") as HTMLSelectElement).value).toBe("video")
    expect((within(dialog).getByLabelText("行业") as HTMLSelectElement).value).toBe("food")
    expect((within(dialog).getByLabelText("门店类型") as HTMLSelectElement).value).toBe("noodle")
    expect((within(dialog).getByLabelText(/做法/) as HTMLTextAreaElement).value).toContain("画面：牛肉面特写")
    expect(within(dialog).getByTestId("example-source").textContent).toContain("巷口面馆")
    expect(within(dialog).getByTestId("example-source").textContent).toContain("红烧牛肉面")
    // Both assets preview through the admin link; nothing is typed by hand in collect mode.
    await waitFor(() => expect(within(dialog).getAllByTestId("asset-tile").length).toBe(2))
    await waitFor(() => expect(dialog.querySelector("video")?.getAttribute("src")).toBe("https://signed/final.mp4"))
    expect(within(dialog).queryByLabelText(/asset_id/)).toBeNull()

    fireEvent.change(within(dialog).getByLabelText("门店类型"), { target: { value: "hotpot" } })
    fireEvent.change(within(dialog).getByLabelText("标签"), { target: { value: "探店，傍晚" } })
    fireEvent.change(within(dialog).getByLabelText("评分"), { target: { value: "5" } })
    fireEvent.change(within(dialog).getByLabelText("说明"), { target: { value: "先门头再上面" } })
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }))
    await waitFor(() => expect(vi.mocked(http.post).mock.calls.some(([path]) => path === "/api/admin/examples")).toBe(true))
    const [, body] = vi.mocked(http.post).mock.calls.find(([path]) => path === "/api/admin/examples") as [string, Record<string, unknown>]
    expect(body).toEqual({
      entry_id: "video",
      industry: "food",
      shop_type: "hotpot",
      subtype: null,
      tags: ["探店", "傍晚"],
      rating: 5,
      status: "active",
      title: "做一条视频",
      notes: "先门头再上面",
      recipe: DRAFT.recipe,
      inputs: [{ asset_id: "asset_photo" }],
      outputs: [{ asset_id: "asset_final" }],
      source: DRAFT.source,
    })
    // Saving closes the sheet and drops the collect parameters from the address.
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
    expect(location).toBe("/app/admin/examples")
  })

  it("refuses to save without an output and explains a server refusal", async () => {
    vi.mocked(http.post).mockImplementation(async (path: string) => {
      if (path.endsWith("/prefill")) return { ...DRAFT, outputs: [] }
      throw new ApiError(422, "EXAMPLE_INVALID", "unknown shop_type for this industry")
    })
    mount("/app/admin/examples?collect=session_1")
    const dialog = await screen.findByRole("dialog")
    await within(dialog).findByLabelText("标题")
    expect(within(dialog).getByText(/没有带回产出/)).toBeTruthy()
    const save = within(dialog).getByRole("button", { name: "保存" }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(within(dialog).getByText("至少要有一条产出")).toBeTruthy()
  })

  it("holds a session with too many outputs until the extras are removed", async () => {
    const outputs = Array.from({ length: 13 }, (_, n) => ({ asset_id: `asset_${n}`, oss_key: `k${n}`, name: `out${n}.png`, mime: "image/png" }))
    vi.mocked(http.post).mockImplementation(async (path: string) => {
      if (path.endsWith("/prefill")) return { ...DRAFT, outputs }
      return { ...DRAFT, id: "lex_new", status: "active" }
    })
    mount("/app/admin/examples?collect=session_1")
    const dialog = await screen.findByRole("dialog")
    await within(dialog).findByLabelText("标题")
    const save = within(dialog).getByRole("button", { name: "保存" }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(within(dialog).getAllByText("产出最多 12 条，现在有 13 条，移除多余的再保存").length).toBeGreaterThan(0)

    fireEvent.click(within(dialog).getByRole("button", { name: "移除 out3.png" }))
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() => expect(vi.mocked(http.post).mock.calls.some(([path]) => path === "/api/admin/examples")).toBe(true))
    const [, body] = vi.mocked(http.post).mock.calls.find(([path]) => path === "/api/admin/examples") as [string, { outputs: { asset_id: string }[] }]
    expect(body.outputs).toHaveLength(12)
    expect(body.outputs.map((output) => output.asset_id)).not.toContain("asset_3")
  })

  it("says what is wrong when the server refuses the fields themselves", async () => {
    vi.mocked(http.post).mockImplementation(async (path: string) => {
      if (path.endsWith("/prefill")) return DRAFT
      throw new ApiError(422, "HTTP_422", "[]")
    })
    mount("/app/admin/examples?collect=session_1")
    const dialog = await screen.findByRole("dialog")
    await within(dialog).findByLabelText("标题")
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }))
    expect(await within(dialog).findByText("内容超出限制或格式不对，检查后再保存")).toBeTruthy()
  })

  it("lets a hand-made example take asset ids and switches the shop types with the industry", async () => {
    vi.mocked(http.post).mockRejectedValue(new ApiError(422, "EXAMPLE_INVALID", "outputs name no known asset"))
    mount("/app/admin/examples")
    fireEvent.click(await screen.findByRole("button", { name: "手工新建" }))
    const dialog = await screen.findByRole("dialog")
    fireEvent.change(within(dialog).getByLabelText("行业"), { target: { value: "beauty" } })
    const shop = within(dialog).getByLabelText("门店类型") as HTMLSelectElement
    expect(Array.from(shop.options).map((option) => option.value)).toEqual(["nail", "other"])
    expect(shop.value).toBe("other")
    fireEvent.change(within(dialog).getByLabelText("标题"), { target: { value: "美甲海报" } })
    fireEvent.change(within(dialog).getByLabelText("入口"), { target: { value: "poster" } })
    fireEvent.change(within(dialog).getByLabelText(/产出 asset_id/), { target: { value: "asset_a\nasset_b, asset_a" } })
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }))
    await waitFor(() => expect(http.post).toHaveBeenCalledTimes(1))
    const [, body] = vi.mocked(http.post).mock.calls[0] as [string, Record<string, unknown>]
    expect(body).toMatchObject({ entry_id: "poster", industry: "beauty", shop_type: "other", outputs: [{ asset_id: "asset_a" }, { asset_id: "asset_b" }] })
    const alert = await within(dialog).findByRole("alert")
    expect(alert.textContent).toContain("outputs name no known asset")
  })
})

describe("form conversions", () => {
  it("splits tags on commas of both widths, trims, dedupes and caps the count", () => {
    expect(splitTags(" 探店 ,傍晚，探店\n人均 30 ")).toEqual(["探店", "傍晚", "人均 30"])
    expect(splitTags(Array.from({ length: 20 }, (_, i) => `t${i}`).join(",")).length).toBe(12)
  })
  it("sends a blank subtype as null and typed ids after listed ones", () => {
    const write = toWrite({ ...EMPTY, title: " x ", outputs: [{ asset_id: "a" }], outputIds: "b a" })
    expect(write).toMatchObject({ title: "x", subtype: null, outputs: [{ asset_id: "a" }, { asset_id: "b" }], inputs: [] })
  })
})

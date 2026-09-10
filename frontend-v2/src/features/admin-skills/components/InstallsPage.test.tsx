import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, useLocation } from "react-router"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { http } from "@/shared/api/http"
import { InstallsPage } from "./InstallsPage"

vi.mock("@/shared/api/http", () => ({
  http: { get: vi.fn(), post: vi.fn() },
  requestBlob: vi.fn(),
}))

const page = {
  total: 1,
  offset: 0,
  limit: 20,
  items: [
    {
      id: "ins-1",
      user: { username: "bo", email: "bo@example.com" },
      catalog_id: "mcp:playwright",
      kind: "mcp",
      title: "Playwright",
      install_dir: "/data/skills/playwright",
      installed_at: "2026-09-02T03:00:00Z",
    },
  ],
}

function Probe() {
  const location = useLocation()
  return <output data-testid="url">{`${location.pathname}${location.search}`}</output>
}

function mount(entry = "/app/admin/skills/installs") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <InstallsPage />
          <Probe />
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("admin-skills")
})

beforeEach(() => {
  vi.mocked(http.get).mockReset().mockResolvedValue(page)
})

afterEach(cleanup)

describe("InstallsPage", () => {
  it("picks up the catalog filter the store handed over, and can drop it", async () => {
    mount("/app/admin/skills/installs?catalog=mcp%3Aplaywright")
    await screen.findByText("Playwright")
    expect(vi.mocked(http.get).mock.calls[0][0]).toBe(
      "/api/admin/skills/installs?catalog_id=mcp%3Aplaywright&offset=0&limit=20",
    )

    fireEvent.click(screen.getByRole("button", { name: "清除技能筛选" }))
    await waitFor(() => expect(screen.getByTestId("url").textContent).toBe("/app/admin/skills/installs"))
  })

  it("states that the list is a record, not a sandbox scan", async () => {
    mount()
    fireEvent.click(screen.getByRole("button", { name: "平台安装记录" }))
    await screen.findByText("Playwright")
    expect(screen.getByText(/这是平台安装历史，不代表仍在安装/)).toBeDefined()
  })

  it("explains an empty list rather than showing a bare table", async () => {
    vi.mocked(http.get).mockResolvedValue({ items: [], total: 0, offset: 0, limit: 20 })
    mount()
    fireEvent.click(screen.getByRole("button", { name: "平台安装记录" }))
    await screen.findByText("还没有安装记录。")
    expect(screen.queryByRole("table")).toBeNull()
  })
})

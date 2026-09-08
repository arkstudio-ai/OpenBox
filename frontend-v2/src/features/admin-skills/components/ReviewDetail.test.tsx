import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { http } from "@/shared/api/http"
import { ReviewDetail } from "./ReviewDetail"

vi.mock("@/shared/api/http", () => ({
  http: { get: vi.fn(), post: vi.fn() },
  requestBlob: vi.fn(),
}))

// A submission is written by a stranger; this one tries to be more than text.
const SKILL_MD = [
  "# 网页调研",
  "",
  "<script>globalThis.__pwned = true</script>",
  "",
  '**加粗** 和 <img src=x onerror="globalThis.__pwned = true">',
].join("\n")

const detail = {
  catalog_id: "community:7",
  kind: "skill",
  origin: "community",
  title: "网页调研",
  name: "web-research",
  icon: "🔎",
  author: { username: "lin", email: "lin@example.com" },
  version: 3,
  published_at: "2026-09-01T02:00:00Z",
  installs_count: 0,
  listing: "pending",
  listing_note: null,
  featured: false,
  is_official: false,
  requires_mcp: ["firecrawl"],
  size: 20480,
  sha256: "9f2c",
  skill_md: SKILL_MD,
  files: [
    { path: "SKILL.md", size: 1024 },
    { path: "scripts/run.py", size: 2048 },
  ],
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <ReviewDetail catalogId="community:7" />
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("admin-skills")
})

beforeEach(() => {
  vi.mocked(http.get).mockReset().mockResolvedValue(detail)
})

afterEach(() => {
  cleanup()
  delete (globalThis as Record<string, unknown>).__pwned
})

describe("ReviewDetail", () => {
  it("shows SKILL.md as characters, not as Markdown or HTML", async () => {
    const { container } = mount()
    const block = await waitFor(() => {
      const found = container.querySelector("pre")
      if (!found) throw new Error("SKILL.md block not rendered yet")
      return found
    })

    // Every byte survives, tags included.
    expect(block.textContent).toBe(SKILL_MD)
    expect(block.className).toContain("font-mono")

    // Nothing in it became part of the document.
    expect(container.querySelector("script")).toBeNull()
    expect(container.querySelector("img")).toBeNull()
    expect(container.querySelector("strong")).toBeNull()
    expect(container.querySelector("h1")).toBeNull()
    expect((globalThis as Record<string, unknown>).__pwned).toBeUndefined()
    expect(screen.getByText("以纯文本展示，不渲染 Markdown、不执行任何内容。")).toBeDefined()
  })

  it("encodes the colon in the catalog id and lists what the archive holds", async () => {
    mount()
    await screen.findByText("scripts/run.py")
    expect(vi.mocked(http.get).mock.calls[0][0]).toBe("/api/admin/skills/review/community%3A7")
    expect(screen.getByText("文件清单（2）")).toBeDefined()
    expect(screen.getByText("firecrawl")).toBeDefined()
    // A pending submission is judged, not re-shelved.
    expect(screen.getByRole("button", { name: "通过" })).toBeDefined()
    expect(screen.getByRole("button", { name: "驳回" })).toBeDefined()
  })

  it("says so when the submission cannot be read", async () => {
    vi.mocked(http.get).mockRejectedValue(new Error("boom"))
    mount()
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("审核详情加载失败。"))
  })

  // A 70 MB ZIP, one declaring 25,000 members, or a corrupt one all arrive with
  // `skill_md: null` and `files: []` — the same shape as a genuinely empty
  // archive. Reading them as "nothing in here" is how unreviewed bytes get
  // approved, so the refusal has to be said out loud.
  it("reports an archive the server refused to open, instead of calling it empty", async () => {
    vi.mocked(http.get).mockResolvedValue({
      ...detail,
      skill_md: null,
      files: [],
      files_total: 0,
      archive_error: "Could not read this archive: BadZipFile",
    })
    mount()
    const alert = await screen.findByRole("alert")
    expect(alert.textContent).toContain("Could not read this archive: BadZipFile")
    expect(screen.queryByText("这个包里没有 SKILL.md。")).toBeNull()
    // No manifest was read, so there is no file list to imply one.
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("counts the members the archive declares, not the ones that fit in the list", async () => {
    vi.mocked(http.get).mockResolvedValue({ ...detail, files_total: 601, files_truncated: true })
    mount()
    await screen.findByText("scripts/run.py")
    expect(screen.getByText("文件清单（601）")).toBeDefined()
    expect(screen.getByText("条目过多，只列出了前面一部分。")).toBeDefined()
  })
})

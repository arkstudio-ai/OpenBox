import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, useLocation } from "react-router"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { http } from "@/shared/api/http"
import { ReviewPage } from "./ReviewPage"

vi.mock("@/shared/api/http", () => ({
  http: { get: vi.fn(), post: vi.fn() },
  requestBlob: vi.fn(),
}))

/** Page three of a 60-deep queue. */
const queue = {
  total: 60,
  offset: 40,
  limit: 20,
  items: [41, 42, 43].map((n) => ({
    catalog_id: `community:${n}`,
    kind: "skill",
    origin: "community",
    title: `投稿 ${n}`,
    name: `submission-${n}`,
    author: { username: "lin", email: "lin@example.com" },
    version: 1,
    published_at: "2026-09-01T02:00:00Z",
    installs_count: 0,
    listing: "pending",
    listing_note: null,
    featured: false,
    is_official: false,
  })),
}

const detail = { ...queue.items[1], skill_md: "# 投稿 42", files: [], files_total: 0 }

function Probe() {
  const location = useLocation()
  return <output data-testid="url">{`${location.pathname}${location.search}`}</output>
}

function mount(entry: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <ReviewPage />
          <Probe />
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  )
}

const url = () => screen.getByTestId("url").textContent

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("admin-skills")
})

beforeEach(() => {
  vi.mocked(http.get)
    .mockReset()
    .mockImplementation((path: string) => Promise.resolve(path.includes("/review/") ? detail : queue))
})

afterEach(cleanup)

describe("ReviewPage", () => {
  // Opening a submission is not a verdict — the row stays in the queue and the
  // next one is the point of being on this page. Sending the operator back to
  // page one on every click makes them re-page for each item they open.
  it("keeps the queue where it was when a submission is opened", async () => {
    mount("/app/admin/skills/review?state=pending&offset=40")
    fireEvent.click(await screen.findByRole("button", { name: /投稿 42/ }))

    await waitFor(() => expect(url()).toContain("id=community%3A42"))
    expect(url()).toContain("offset=40")
    await waitFor(() =>
      expect(vi.mocked(http.get).mock.calls.map(([path]) => path)).toContain(
        "/api/admin/skills/review?state=pending&offset=40&limit=20",
      ),
    )
    // Nothing was re-fetched from the top of the list.
    expect(vi.mocked(http.get).mock.calls.map(([path]) => path)).not.toContain(
      "/api/admin/skills/review?state=pending&offset=0&limit=20",
    )
  })

  it("does reset the page when the queue itself changes", async () => {
    mount("/app/admin/skills/review?state=pending&offset=40&id=community%3A42")
    fireEvent.click(await screen.findByRole("button", { name: "已驳回" }))
    await waitFor(() => expect(url()).toBe("/app/admin/skills/review?state=rejected"))
  })
})

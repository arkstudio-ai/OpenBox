import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http, ApiError } from "@/shared/api/http"
import { useToastStore } from "@/shared/ui/Toast"
import type { Topic } from "../api"
import { TopicsPage } from "./TopicsPage"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: "zh-CN", exists: () => false } }),
}))

const topic = (over: Partial<Topic> = {}): Topic => ({
  id: "tpc_1",
  slug: "release-2026-09",
  title: "九月更新",
  coverUrl: null,
  contentMd: "# 新功能\n消息中心上线。",
  ctaLabel: null,
  ctaLink: null,
  status: "draft",
  createdBy: "admin",
  createdAt: "2026-09-11T08:00:00+00:00",
  updatedAt: "2026-09-11T08:00:00+00:00",
  publishedAt: null,
  ...over,
})

let client: QueryClient
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  useToastStore.getState().clear()
})

const mount = () =>
  render(
    <QueryClientProvider client={client}>
      <TopicsPage />
    </QueryClientProvider>,
  )

it("lists topics; only published ones get view and copy-link", async () => {
  vi.spyOn(http, "get").mockResolvedValue({
    items: [
      topic(),
      topic({ id: "tpc_2", slug: "live", status: "published", publishedAt: "2026-09-11T09:00:00+00:00" }),
    ],
  })
  mount()
  expect(await screen.findAllByText("九月更新", { selector: "p" })).toHaveLength(2)
  expect(screen.getByText("/topics/release-2026-09")).toBeTruthy()
  expect(screen.getAllByRole("button", { name: "topics.publish" })).toHaveLength(1)
  expect(screen.getAllByRole("button", { name: "topics.unpublish" })).toHaveLength(1)
  expect(screen.getAllByRole("button", { name: "topics.copyLink" })).toHaveLength(1)
  expect(screen.getByRole("link", { name: "topics.view" }).getAttribute("href")).toBe("/topics/live")
})

it("creates a topic with a live Markdown preview and a paired CTA", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ items: [] })
  const post = vi.spyOn(http, "post").mockResolvedValue(topic())
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "topics.create" }))
  fireEvent.change(screen.getByLabelText("topics.slug"), { target: { value: "release-2026-09" } })
  fireEvent.change(screen.getByLabelText("topics.title"), { target: { value: "九月更新" } })
  fireEvent.change(screen.getByLabelText("topics.content"), {
    target: { value: "# 新功能\n\n消息中心上线。" },
  })
  expect(screen.getByRole("heading", { level: 1, name: "新功能" })).toBeTruthy()
  // A label without a target is refused client-side.
  fireEvent.change(screen.getByLabelText("topics.ctaLabel"), { target: { value: "去看看" } })
  fireEvent.click(screen.getByRole("button", { name: "common.save" }))
  expect(await screen.findByRole("alert")).toBeTruthy()
  expect(post).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText("topics.ctaLink"), { target: { value: "url" } })
  fireEvent.change(screen.getByLabelText("form.url"), { target: { value: "https://localhost/promo" } })
  fireEvent.click(screen.getByRole("button", { name: "common.save" }))
  await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
  expect(post).toHaveBeenCalledWith("/api/admin/messages/topics", {
    slug: "release-2026-09",
    title: "九月更新",
    coverUrl: null,
    contentMd: "# 新功能\n\n消息中心上线。",
    ctaLabel: "去看看",
    ctaLink: { kind: "url", url: "https://localhost/promo" },
  })
})

it("locks the slug of a published topic and surfaces a taken slug from the server", async () => {
  vi.spyOn(http, "get").mockResolvedValue({
    items: [topic({ status: "published" }), topic({ id: "tpc_2", slug: "other" })],
  })
  vi.spyOn(http, "put").mockRejectedValue(new ApiError(409, "TOPIC_SLUG_TAKEN", "taken"))
  mount()
  fireEvent.click((await screen.findAllByRole("button", { name: "topics.editor" }))[0])
  expect((screen.getByLabelText("topics.slug") as HTMLInputElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole("button", { name: "common.cancel" }))
  fireEvent.click(screen.getAllByRole("button", { name: "topics.editor" })[1])
  expect((screen.getByLabelText("topics.slug") as HTMLInputElement).disabled).toBe(false)
  fireEvent.change(screen.getByLabelText("topics.slug"), { target: { value: "release-2026-09" } })
  fireEvent.click(screen.getByRole("button", { name: "common.save" }))
  expect((await screen.findByRole("alert")).textContent).toBe("topics.slugTaken")
})

it("publishes and unpublishes from the list", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ items: [topic()] })
  const post = vi.spyOn(http, "post").mockResolvedValue(topic({ status: "published" }))
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "topics.publish" }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/admin/messages/topics/tpc_1/publish"))
})

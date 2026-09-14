import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useToastStore } from "@/shared/ui/Toast"
import type { Announcement } from "../api"
import { AnnouncementsPage } from "./AnnouncementsPage"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (k: string, opts?: Record<string, unknown>) => (opts?.count !== undefined ? `${k}:${opts.count}` : k),
    i18n: { language: "zh-CN", exists: () => false },
  }),
}))

const row = (over: Partial<Announcement> = {}): Announcement => ({
  id: "ann_1",
  status: "draft",
  title: "版本更新",
  body: "新版本已上线",
  link: { kind: "topic", slug: "release-2026-09" },
  audience: { kind: "all" },
  push: false,
  publishAt: null,
  expiresAt: null,
  publishedAt: null,
  fanoutAt: null,
  fanoutCount: 0,
  createdBy: "admin",
  createdAt: "2026-09-11T08:00:00+00:00",
  updatedAt: "2026-09-11T08:00:00+00:00",
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
      <AnnouncementsPage />
    </QueryClientProvider>,
  )

it("lists announcements with status, audience and link", async () => {
  vi.spyOn(http, "get").mockResolvedValue({
    items: [row(), row({ id: "ann_2", status: "published", fanoutCount: 12 })],
  })
  mount()
  expect(await screen.findAllByText("版本更新", { selector: "p" })).toHaveLength(2)
  expect(screen.getByText("status.draft")).toBeTruthy()
  expect(screen.getByText("status.published")).toBeTruthy()
  expect(screen.getByText("list.fanout:12")).toBeTruthy()
  expect(screen.getAllByText("list.audience.all")).toHaveLength(2)
  // Drafts can be edited and published; published rows only revoked/previewed.
  expect(screen.getAllByRole("button", { name: "action.edit" })).toHaveLength(1)
  expect(screen.getAllByRole("button", { name: "action.revoke" })).toHaveLength(1)
  expect(screen.getAllByRole("button", { name: "action.preview" })).toHaveLength(2)
})

it("creates a draft from the form with the audience and link the operator picked", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ items: [] })
  const post = vi.spyOn(http, "post").mockResolvedValue(row())
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "list.create" }))
  fireEvent.change(screen.getByLabelText("form.title"), { target: { value: "  版本更新 " } })
  fireEvent.change(screen.getByLabelText("form.body"), { target: { value: "新版本已上线" } })
  fireEvent.change(screen.getByLabelText("form.link"), { target: { value: "topic" } })
  fireEvent.change(screen.getByLabelText("form.topicSlug"), { target: { value: "release-2026-09" } })
  fireEvent.change(screen.getByLabelText("form.audience"), { target: { value: "users" } })
  fireEvent.change(screen.getByLabelText("form.userIds"), { target: { value: "u1\nu2, u1" } })
  fireEvent.click(screen.getByRole("button", { name: "common.save" }))
  await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
  expect(post).toHaveBeenCalledWith("/api/admin/messages/announcements", {
    title: "版本更新",
    body: "新版本已上线",
    link: { kind: "topic", slug: "release-2026-09" },
    audience: { kind: "users", ids: ["u1", "u2"] },
    push: false,
    publishAt: null,
    expiresAt: null,
  })
  expect(useToastStore.getState().items.map((item) => item.text)).toContain("form.saved")
})

it("refuses an http link or an empty title before calling the server", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ items: [] })
  const post = vi.spyOn(http, "post")
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "list.create" }))
  fireEvent.click(screen.getByRole("button", { name: "common.save" }))
  expect(await screen.findByRole("alert")).toBeTruthy()
  fireEvent.change(screen.getByLabelText("form.title"), { target: { value: "x" } })
  fireEvent.change(screen.getByLabelText("form.link"), { target: { value: "url" } })
  fireEvent.change(screen.getByLabelText("form.url"), { target: { value: "http://evil.example/" } })
  fireEvent.click(screen.getByRole("button", { name: "common.save" }))
  expect(await screen.findByRole("alert")).toBeTruthy()
  expect(post).not.toHaveBeenCalled()
})

it("confirms with the live recipient count before publishing, and revokes with a warning", async () => {
  vi.spyOn(http, "get").mockImplementation((path: string) =>
    Promise.resolve(
      path.endsWith("/announcements/ann_1")
        ? { ...row(), recipientCount: 42 }
        : { items: [row(), row({ id: "ann_2", status: "published" })] },
    ),
  )
  const post = vi
    .spyOn(http, "post")
    .mockImplementation((path: string) =>
      Promise.resolve(
        path.endsWith("/revoke")
          ? { ...row({ status: "revoked" }), hidden: 7 }
          : row({ status: "published" }),
      ),
    )
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "action.publish" }))
  expect(await screen.findByText("action.publishConfirmBody:42")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "action.confirm" }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/admin/messages/announcements/ann_1/publish"))
  fireEvent.click(screen.getByRole("button", { name: "action.revoke" }))
  expect(await screen.findByText("action.revokeConfirmBody")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "action.confirm" }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/admin/messages/announcements/ann_2/revoke"))
  await waitFor(() =>
    expect(useToastStore.getState().items.map((item) => item.text)).toContain("action.revoked:7"),
  )
})

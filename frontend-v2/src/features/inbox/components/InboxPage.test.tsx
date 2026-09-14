import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { InboxItem, InboxPage as Page } from "@/shared/api/inbox"
import { InboxPage } from "./InboxPage"

const navigate = vi.fn()
vi.mock("react-router", () => ({ useNavigate: () => navigate }))
vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (k: string, opts?: Record<string, unknown>) => (opts?.name ? `${k}:${opts.name}` : k),
    i18n: { language: "zh-CN", exists: () => false },
  }),
}))
vi.mock("@/shared/ws/client", () => ({ wsClient: { on: () => () => undefined } }))

const item = (id: string, over: Partial<InboxItem> = {}): InboxItem => ({
  id,
  category: "session",
  kind: "task_completed",
  title: `Title ${id}`,
  body: `Body ${id}`,
  link: { kind: "session", workspaceId: "home", sessionId: `sess-${id}` },
  workspaceId: "home",
  announcementId: null,
  readAt: null,
  resolvedAt: null,
  expiresAt: null,
  createdAt: "2026-09-11T08:00:00+00:00",
  ...over,
})
const unread = { total: 3, session: 2, system: 1, notice: 0 }
const pages: Record<string, Page> = {
  "": {
    items: [
      item("a"),
      item("b", { category: "system", kind: "publish_done", workspaceId: "team" }),
      item("c", { readAt: "2026-09-11T00:00:00+00:00" }),
    ],
    nextCursor: "more",
    unread,
  },
  session: {
    items: [item("a"), item("c", { readAt: "2026-09-11T00:00:00+00:00" })],
    nextCursor: null,
    unread,
  },
  system: {
    items: [item("b", { category: "system", kind: "publish_done", workspaceId: "team" })],
    nextCursor: null,
    unread,
  },
  notice: { items: [], nextCursor: null, unread },
}

let client: QueryClient
let requests: string[]
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  requests = []
  useAuthStore.setState({ isAuthenticated: true, user: { id: "u1", username: "U", role: "user" } as never })
  useWorkspaceStore.setState({
    currentId: "home",
    items: [
      { id: "home", name: "Home", owner_user_id: "u1", kind: "personal", role: "owner" },
      { id: "team", name: "Team", owner_user_id: "b", kind: "team", role: "member" },
    ],
  })
  vi.spyOn(http, "get").mockImplementation((path: string) => {
    requests.push(path)
    if (path === "/api/inbox/unread") return Promise.resolve(unread)
    if (path.startsWith("/api/inbox?")) {
      const params = new URLSearchParams(path.slice("/api/inbox?".length))
      const category = params.get("category") ?? ""
      if (params.get("cursor")) return Promise.resolve({ items: [item("d")], nextCursor: null, unread })
      return Promise.resolve(pages[category])
    }
    return Promise.resolve({})
  })
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  navigate.mockClear()
  useAuthStore.getState().clearAuth()
})

const mount = () =>
  render(
    <QueryClientProvider client={client}>
      <InboxPage />
    </QueryClientProvider>,
  )

it("lists rows with unread dots, a foreign workspace chip and tab counts", async () => {
  mount()
  expect(await screen.findByText("Title a")).toBeTruthy()
  expect(screen.getByTestId("inbox-unread-a")).toBeTruthy()
  expect(screen.queryByTestId("inbox-unread-c")).toBeNull()
  expect(screen.getByText("workspace:Team")).toBeTruthy()
  await waitFor(() => expect(screen.getByRole("button", { name: "tabs.all 3" })).toBeTruthy())
  expect(screen.getByRole("button", { name: "tabs.session 2" })).toBeTruthy()
  expect(screen.getByRole("button", { name: "tabs.notice" })).toBeTruthy()
})

it("switches tabs by category and loads the next page on demand", async () => {
  mount()
  await screen.findByText("Title a")
  fireEvent.click(screen.getByRole("button", { name: "tabs.notice" }))
  expect(await screen.findByText("empty")).toBeTruthy()
  expect(requests.at(-1)).toContain("category=notice")
  fireEvent.click(screen.getByRole("button", { name: "tabs.all 3" }))
  fireEvent.click(await screen.findByRole("button", { name: "loadMore" }))
  expect(await screen.findByText("Title d")).toBeTruthy()
  expect(requests.some((path) => path.includes("cursor=more"))).toBe(true)
})

it("tapping an unread row marks it read on the server, verifies the session and navigates", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue(item("a", { readAt: "2026-09-11T09:00:00+00:00" }))
  mount()
  fireEvent.click(await screen.findByTestId("inbox-a"))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/inbox/a/read"))
  await waitFor(() => expect(navigate).toHaveBeenCalledWith("/app/s/sess-a"))
  expect(requests).toContain("/api/agent/session/sess-a")
})

it("a read row opens without another receipt; mark all read posts for the tab", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ updated: 2 })
  mount()
  fireEvent.click(await screen.findByTestId("inbox-c"))
  await waitFor(() => expect(navigate).toHaveBeenCalledWith("/app/s/sess-c"))
  expect(post).not.toHaveBeenCalledWith("/api/inbox/c/read")
  fireEvent.click(screen.getByRole("button", { name: "readAll" }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/inbox/read-all"))
})

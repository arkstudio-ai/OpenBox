import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http, ApiError } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { TopicPage as Topic } from "@/shared/api/inbox"
import { TopicPage } from "./TopicPage"

const navigate = vi.fn()
vi.mock("react-router", () => ({ useNavigate: () => navigate }))
vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: "zh-CN", exists: () => false } }),
}))

const topic = (over: Partial<Topic> = {}): Topic => ({
  id: "tpc_1",
  slug: "release-2026-09",
  title: "九月更新",
  coverUrl: null,
  contentMd: "# 新功能\n\n消息中心上线。",
  ctaLabel: "去看看",
  ctaLink: { kind: "skills", workspaceId: "home" },
  publishedAt: "2026-09-11T09:00:00+00:00",
  updatedAt: "2026-09-11T09:00:00+00:00",
  ...over,
})

let client: QueryClient
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useWorkspaceStore.setState({
    currentId: "home",
    items: [{ id: "home", name: "Home", owner_user_id: "u1", kind: "personal", role: "owner" }],
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
      <TopicPage slug="release-2026-09" />
    </QueryClientProvider>,
  )

it("renders the published topic as Markdown", async () => {
  vi.spyOn(http, "get").mockResolvedValue(topic())
  mount()
  expect(await screen.findByRole("heading", { level: 1, name: "九月更新" })).toBeTruthy()
  expect(screen.getByRole("heading", { level: 1, name: "新功能" })).toBeTruthy()
  expect(screen.getByText("消息中心上线。")).toBeTruthy()
})

it("shows not-found for drafts and unknown slugs", async () => {
  vi.spyOn(http, "get").mockRejectedValue(new ApiError(404, "NOT_FOUND", "nope"))
  mount()
  expect(await screen.findByText("topic.notFound")).toBeTruthy()
})

it("asks a signed-out visitor to sign in before an in-app CTA", async () => {
  vi.spyOn(http, "get").mockResolvedValue(topic())
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "topic.signIn" }))
  expect(navigate).toHaveBeenCalledWith("/login")
})

it("opens the CTA through the same allow-listed resolver when signed in", async () => {
  useAuthStore.setState({ isAuthenticated: true, user: { id: "u1", username: "U", role: "user" } as never })
  vi.spyOn(http, "get").mockResolvedValue(topic())
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "去看看" }))
  await waitFor(() => expect(navigate).toHaveBeenCalledWith("/app/skills"))
})

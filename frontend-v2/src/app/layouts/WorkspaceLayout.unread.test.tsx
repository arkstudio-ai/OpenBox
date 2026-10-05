// Route selection and real Query ownership; unrelated widgets are placeholders.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createMemoryRouter, RouterProvider } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { paths, routePatterns } from "@/shared/router/paths"
import { useAssistantSnapshot } from "@/features/chat/api/assistant"
import WorkspaceLayout from "./WorkspaceLayout"

vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() },
}))
vi.mock("@/shared/ws/client", () => ({ wsClient: { on: () => () => undefined } }))
vi.mock("@/features/chat", async () => {
  const assistant = await import("@/features/chat/api/assistant")
  const sessions = await import("@/features/chat/api/message-actions")
  return { useAssistantSidebarUnread: assistant.useAssistantSidebarUnread, useSessionQuery: sessions.useSessionQuery }
})
vi.mock("@/features/workspace", () => ({
  Sidebar: ({ assistantUnread }: { assistantUnread?: { count: number; lowerBound: boolean } }) =>
    <aside data-testid="sidebar">{assistantUnread && <output>{`${assistantUnread.count}:${assistantUnread.lowerBound}`}</output>}</aside>,
  Topbar: () => null, useWorkspaceEvents: () => undefined,
  useWorkspaceUi: (selector: (state: { setLastSession: () => void }) => unknown) => selector({ setLastSession: () => undefined }),
}))
vi.mock("@/features/workbench", () => ({
  DesktopActivationDialog: () => null, WorkbenchPanel: () => null, usePanelEvents: () => undefined,
  usePanelStore: () => false,
}))
vi.mock("@/features/cron", () => ({ CronSidebarJobs: () => null, CronStatusPill: () => null }))
vi.mock("@/features/memory", () => ({ MemoryPauseToggle: () => null }))
vi.mock("@/features/inbox", () => ({ useInboxLiveEvents: () => undefined }))
vi.mock("@/shared/appearance/store", () => ({
  useAppearanceStore: Object.assign(() => false, { getState: () => ({ hydrateFromServer: () => undefined }) }),
}))

let client: QueryClient
const calls = (path: string) => vi.mocked(http.get).mock.calls.filter(([url]) => url === path)
function MainEntry() {
  const read = useAssistantSnapshot()
  return <p>{read.data?.session?.id}</p>
}
function mount(entry: string) {
  const router = createMemoryRouter([{ path: paths.app, element: <WorkspaceLayout />, children: [
    { index: true, element: <p>new chat</p> },
    { path: routePatterns.chat, element: <p>existing chat</p> },
    { path: routePatterns.assistant, element: <MainEntry /> },
    { path: routePatterns.settings, element: <p>settings</p> },
    { path: `${routePatterns.admin}/*`, element: <p>admin</p> },
    { path: routePatterns.memory, element: <p>memory</p> },
    { path: routePatterns.wiki, element: <p>wiki</p> },
  ] }], { initialEntries: [entry] })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
}
beforeEach(() => {
  vi.clearAllMocks()
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  vi.mocked(http.get).mockImplementation(async (path) => {
    if (path === "/api/workspaces") return { items: [{ id: "workspace", name: "Test", owner_user_id: "owner", kind: "personal", role: "owner" }], default_workspace_id: "workspace" }
    if (path === "/api/assistant/unread") return { unread_count: 3, unread_count_is_lower_bound: false }
    if (path === "/api/assistant") return { state: "ready", session: { id: "main" }, unread_count: 1, unread_count_is_lower_bound: false }
    if (path.startsWith("/api/agent/session/")) return { id: "chat", kind: "normal", visibility: "workspace", memory_policy: "standard", status: "idle" }
    return {}
  })
})
afterEach(() => { cleanup(); client.clear(); useWorkspaceStore.getState().clear() })

it("the new ordinary chat reads a count without fetching full assistant tasks or display tokens", async () => {
  mount(paths.app)
  await waitFor(() => expect(screen.getByText("3:false")).toBeTruthy())
  expect(calls("/api/assistant/unread")).toHaveLength(1)
  expect(calls("/api/assistant")).toHaveLength(0)
})

it.each(["normal", "assistant", "assistant_managed"])(
  "an existing %s session waits for its real audience before selecting the badge query", async (kind) => {
    const original = vi.mocked(http.get).getMockImplementation()!
    let finish!: (value: unknown) => void
    const pending = new Promise<unknown>((resolve) => { finish = resolve })
    vi.mocked(http.get).mockImplementation((path, options) => path === "/api/agent/session/chat"
      ? pending : original(path, options))
    mount(paths.chat("chat"))
    await waitFor(() => expect(calls("/api/agent/session/chat")).toHaveLength(1))
    expect(calls("/api/assistant/unread")).toHaveLength(0)
    expect(calls("/api/assistant")).toHaveLength(0)
    await act(async () => finish({ id: "chat", kind: kind === "assistant" ? "assistant" : "normal",
      assistant_managed: kind === "assistant_managed", visibility: kind === "normal" ? "workspace" : "private", status: "idle" }))
    await waitFor(() => expect(screen.getByText(kind === "assistant" ? "1:false" : "3:false")).toBeTruthy())
    expect(calls("/api/assistant/unread")).toHaveLength(kind === "assistant" ? 0 : 1)
    expect(calls("/api/assistant")).toHaveLength(kind === "assistant" ? 1 : 0)
  },
)

it("the fixed assistant route and layout share one full request without an unread request", async () => {
  mount(paths.assistant)
  await waitFor(() => expect(screen.getByText("main")).toBeTruthy())
  expect(screen.getByText("1:false")).toBeTruthy()
  expect(calls("/api/assistant")).toHaveLength(1)
  expect(calls("/api/assistant/unread")).toHaveLength(0)
})

it.each([paths.settings(), paths.adminFleet, paths.memory, paths.wiki()])(
  "%s does not poll an assistant badge on a hidden or observation sidebar", async (path) => {
    mount(path)
    await waitFor(() => expect(calls("/api/workspaces")).toHaveLength(1))
    await act(async () => { await Promise.resolve() })
    expect(calls("/api/assistant")).toHaveLength(0)
    expect(calls("/api/assistant/unread")).toHaveLength(0)
  },
)

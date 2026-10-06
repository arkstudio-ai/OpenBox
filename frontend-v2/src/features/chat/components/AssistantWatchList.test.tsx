import type { PropsWithChildren } from "react"
import { act, cleanup, fireEvent, render, renderHook, screen, within } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Route, Routes, useParams } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useAssistantWatch, type AssistantWatchItem } from "../api/assistant-watch"
import { AssistantWatchList } from "./AssistantWatchList"

type Frame = Record<string, unknown>
const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<(data: Frame) => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { on: (event: string, callback: (data: Frame) => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() },
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

function watched(id: string, extra: Partial<AssistantWatchItem> = {}): AssistantWatchItem {
  return { task_id: `task-${id}`, title: `Conversation ${id}`, project: { id: "project", name: `Project ${id}` },
    session_id: `session-${id}`, session_status: "idle", desired_state: "running", observed_state: "idle",
    revision: 1, updated_at: "2026-10-06T00:00:00Z", pending_questions: 0, ...extra }
}
let page: { items: AssistantWatchItem[]; has_more: boolean } | Record<string, never>
let client: QueryClient
beforeEach(() => {
  vi.clearAllMocks()
  listeners.clear()
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  page = { items: [], has_more: false }
  vi.mocked(http.get).mockImplementation(async (url) => {
    if (url === "/api/assistant/watch") return page
    throw new Error(`Unexpected read: ${url}`)
  })
})
afterEach(() => { cleanup(); client.clear(); vi.useRealTimers() })
const reads = () => vi.mocked(http.get).mock.calls.filter(([url]) => url === "/api/assistant/watch").length

function Conversation() {
  return <p>{`conversation:${useParams().sessionId}`}</p>
}
function mount() {
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/app"]}>
    <AssistantWatchList />
    <Routes><Route path="/app" element={<p>home</p>} /><Route path="/app/s/:sessionId" element={<Conversation />} />
      <Route path="/app/assistant" element={<p>assistant page</p>} /></Routes>
  </MemoryRouter></QueryClientProvider>)
}

describe("sidebar watch list", () => {
  it("lists watched conversations with a status dot, the project on hover and a pending-question count", async () => {
    page = { has_more: false, items: [
      watched("a", { session_status: "busy", observed_state: "running" }),
      watched("b", { session_status: "idle", observed_state: "waiting_input", pending_questions: 2 }),
      watched("c", { observed_state: "completed" }),
      watched("d", { session_status: "error", observed_state: "error" }),
    ] }
    mount()
    const list = await screen.findByTestId("assistant-watch-list")
    const rows = within(list).getAllByRole("link")
    expect(rows.map((row) => row.textContent)).toEqual(["Conversation a", "Conversation b2", "Conversation c", "Conversation d"])
    expect(rows.map((row) => row.getAttribute("title"))).toEqual(["Project a", "Project b", "Project c", "Project d"])
    expect(rows.map((row) => within(row).getByRole("img").getAttribute("aria-label"))).toEqual([
      "assistant.watch.state.running", "assistant.watch.state.waiting_input",
      "assistant.watch.state.completed", "assistant.watch.state.error"])
    expect(within(rows[1]).getByLabelText("assistant.watch.pending").textContent).toBe("2")
    expect(within(rows[0]).queryByLabelText("assistant.watch.pending")).toBeNull()
    expect(screen.queryByText("assistant.watch.more")).toBeNull()
  })

  it.each([["an empty list", { items: [], has_more: false }], ["no assistant yet (an older server)", {}]])(
    "is hidden for %s", async (_label, value) => {
      page = value as typeof page
      mount()
      await act(async () => { await Promise.resolve() })
      expect(reads()).toBe(1)
      expect(screen.queryByTestId("assistant-watch-list")).toBeNull()
    })

  it("opens the original conversation and marks it current", async () => {
    page = { has_more: false, items: [watched("a"), watched("b")] }
    mount()
    fireEvent.click(await screen.findByRole("link", { name: /Conversation b/ }))
    expect(await screen.findByText("conversation:session-b")).toBeTruthy()
    expect(screen.getByRole("link", { name: /Conversation b/ }).getAttribute("aria-current")).toBe("page")
    expect(screen.getByRole("link", { name: /Conversation a/ }).getAttribute("aria-current")).toBeNull()
  })

  it.each([
    ["more than eight rows", { items: Array.from({ length: 10 }, (_, i) => watched(String(i))), has_more: false }, 8],
    ["more on the server", { items: [watched("a"), watched("b")], has_more: true }, 2],
  ])("shows at most eight rows and links the rest to the assistant page for %s", async (_label, value, shown) => {
    page = value
    mount()
    const list = await screen.findByTestId("assistant-watch-list")
    const more = within(list).getByRole("link", { name: "assistant.watch.more" })
    expect(within(list).getAllByRole("link")).toHaveLength(shown + 1)
    expect(more.getAttribute("href")).toBe("/app/assistant")
  })
})

describe("watch list freshness", () => {
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
  const emit = (event: string, data: Frame = {}) => act(() => { listeners.get(event)?.forEach((callback) => callback(data)) })
  beforeEach(() => {
    vi.useFakeTimers()
    page = { has_more: false, items: [watched("a")] }
  })

  it("re-reads every 30 seconds when nothing happens, never faster", async () => {
    renderHook(() => useAssistantWatch(), { wrapper })
    await tick(10)
    expect(reads()).toBe(1)
    await tick(29_000)
    expect(reads()).toBe(1)
    await tick(1_100)
    expect(reads()).toBe(2)
  })

  it("refreshes once per burst of relevant socket hints and ignores unrelated ones", async () => {
    renderHook(() => useAssistantWatch(), { wrapper })
    await tick(10)
    emit("session.status", { sessionId: "unwatched", status: "busy" })
    emit("question.asked", { session_id: "unwatched" })
    await tick(1_000)
    expect(reads()).toBe(1)
    // A watched conversation started running: its dot must change.
    emit("session.status", { sessionId: "session-a", status: "busy" })
    await tick(1_000)
    expect(reads()).toBe(2)
    // Any run settling (the assistant may have started or stopped watching) — once per burst.
    for (let i = 0; i < 5; i++) emit("session.status", { sessionId: `other-${i}`, status: "idle" })
    await tick(1_000)
    expect(reads()).toBe(3)
    emit("question.asked", { session_id: "session-a" })
    await tick(1_000)
    expect(reads()).toBe(4)
    emit("__connected")
    await tick(1_000)
    expect(reads()).toBe(5)
  })

  it("does not read for a signed-out viewer or a disabled sidebar", async () => {
    renderHook(() => useAssistantWatch(false), { wrapper })
    await tick(31_000)
    emit("session.status", { sessionId: "session-a", status: "idle" })
    await tick(1_000)
    expect(reads()).toBe(0)
    expect([...listeners.values()].every((set) => set.size === 0)).toBe(true)
  })
})

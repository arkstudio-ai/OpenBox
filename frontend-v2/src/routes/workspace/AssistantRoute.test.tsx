import type { PropsWithChildren } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { useStreamStore } from "@/features/chat"
import AssistantRoute from "./AssistantRoute"

type Frame = { sessionId: string; generation?: number; [key: string]: unknown }
const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<(data: Frame) => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { connect: vi.fn(), on: (event: string, callback: (data: Frame) => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn(), post: vi.fn() },
}))
vi.mock("react-i18next", async (original) => ({ ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
// The page, its transcript and the socket bridge are real; the composer and
// the resource picker are unrelated to how replies arrive.
vi.mock("@/features/chat", async (original) => ({ ...await original<typeof import("@/features/chat")>(), Composer: () => null }))
vi.mock("@/features/resources", () => ({ useResourceMention: () => undefined }))
vi.mock("@/features/chat/components/Markdown", () => ({ default: ({ text }: { text: string }) => <p>{text}</p> }))

const session = { id: "main", user_id: "owner", workspace_id: "workspace", project_id: "project", kind: "assistant",
  agent: "assistant", model: "test/model", variant: null, status: "idle", title: "Assistant",
  created_at: "2026-10-06T00:00:00Z", updated_at: "2026-10-06T00:00:00Z" }
const snapshot = { state: "ready", session, high_water_mark: 0, event_cursor: "cursor", last_seen_sequence: 0, tasks: [],
  next_task_cursor: null, answers: [], next_before_sequence: null, unread_count: 0, unread_count_is_lower_bound: false }
const at = (second: number) => `2026-10-06T00:00:${String(second).padStart(2, "0")}Z`
const text = (id: string, value: string, extra: object = {}) => ({ id, type: "text" as const, text: value, ...extra })
const user = (id: string, second: number, part: ReturnType<typeof text>): MessageWithParts =>
  ({ id, session_id: "main", role: "user", created_at: at(second), parts: [part] })
const reply = (id: string, second: number, value: string): MessageWithParts =>
  ({ id, session_id: "main", role: "assistant", created_at: at(second), finish: "stop", parts: [text(`${id}-text`, value, { channel: "final" })] })
let history: MessageWithParts[]
let client: QueryClient

function route(url: string) {
  if (url === "/api/assistant?answer_scope=unread") return snapshot
  if (url.startsWith("/api/assistant/events?")) return { state: "ready", assistant_session_id: "main", next_cursor: "cursor",
    next_sequence: 0, events: [], has_more: false }
  if (url.startsWith("/api/assistant/requests?")) return { items: [], next_cursor: null, receipts: [] }
  if (url === "/api/agent/session/main") return session
  if (url.startsWith("/api/agent/session/main/history?")) return { messages: history, has_more: false }
  if (url === "/api/agent/permission" || url === "/api/agent/question" || url === "/api/agent/agent") return []
  if (url === "/api/agent/config") return {}
  throw new Error(`Unexpected read: ${url}`)
}

beforeEach(() => {
  listeners.clear()
  history = [user("question", 1, text("question-text", "Summarize my day", { origin: "human" }))]
  useAuthStore.setState({ user: { id: "owner", username: "owner", role: "user" } })
  useWorkspaceStore.setState({ currentId: "workspace" })
  useStreamStore.getState().clearMessages("main")
  useStreamStore.setState({ status: new Map(), statusGeneration: new Map(), terminalStatusGeneration: new Map() })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(http.get).mockImplementation(async (url) => route(url))
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal("IntersectionObserver", class { observe() {} disconnect() {} })
})
afterEach(() => { cleanup(); client.clear(); vi.clearAllMocks(); vi.unstubAllGlobals() })

function mount() {
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
  return render(<AssistantRoute />, { wrapper })
}
const emit = (event: string, data: Frame) => act(() => { listeners.get(event)?.forEach((callback) => callback(data)) })
const reads = (part: string) => vi.mocked(http.get).mock.calls.filter(([url]) => url.includes(part))

it("streams the main session's reply into the page as its deltas arrive", async () => {
  const view = mount()
  await screen.findByText("Summarize my day")
  emit("session.status", { sessionId: "main", generation: 1, status: "busy" })
  emit("message.created", { sessionId: "main", generation: 1, message: { id: "reply", session_id: "main", role: "assistant",
    created_at: at(2), parts: [text("reply-text", "", { channel: "final" })] } })
  emit("message.text_delta", { sessionId: "main", generation: 1, messageId: "reply", partId: "reply-text", text: "Three meetings, " })
  expect(await screen.findByText("Three meetings,")).toBeTruthy()
  emit("part.delta", { sessionId: "main", generation: 1, messageId: "reply", partId: "reply-text", delta: "one deadline." })
  expect(await screen.findByText("Three meetings, one deadline.")).toBeTruthy()
  expect(view.container.textContent).not.toContain("assistant.source")
  expect(reads("/api/assistant/messages")).toHaveLength(0)
})

it("keeps report and recovery inputs out of the transcript while showing the replies they produced", async () => {
  history = [
    ...history,
    reply("answer", 2, "I asked a task session to check."),
    user("report-input", 3, text("report-text", "Summarize this execution result.", { synthetic: true, origin: "task_result" })),
    reply("report", 4, "The check finished: nothing is overdue."),
    user("recovery-input", 5, text("recovery-text", "Continue after the interruption.", { synthetic: true, origin: "system_recovery" })),
    reply("recovered", 6, "Picking up where I left off."),
  ]
  const view = mount()
  expect(await screen.findByText("The check finished: nothing is overdue.")).toBeTruthy()
  expect(screen.getByText("I asked a task session to check.")).toBeTruthy()
  expect(screen.getByText("Picking up where I left off.")).toBeTruthy()
  expect(view.container.textContent).not.toContain("Summarize this execution result.")
  expect(view.container.textContent).not.toContain("Continue after the interruption.")
  expect(view.container.textContent).not.toContain("assistant.source")
  await waitFor(() => expect(reads("/history?").length).toBeGreaterThan(0))
  expect(reads("/api/assistant/messages")).toHaveLength(0)
})

import type { PropsWithChildren } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import type { QuestionRequest } from "@/shared/types/api"
import { usePendingStore, useStreamStore } from "@/features/chat"
import AssistantRoute from "./AssistantRoute"

type Frame = { sessionId: string; generation?: number; [key: string]: unknown }
const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<(data: Frame) => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { connect: vi.fn(), on: (event: string, callback: (data: Frame) => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}))
vi.mock("react-i18next", async (original) => ({ ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
// The page, its transcript and the socket bridge are real; the composer and
// the resource picker are unrelated to how replies arrive.
vi.mock("@/features/chat", async (original) => ({ ...await original<typeof import("@/features/chat")>(),
  Composer: ({ draft, suggestions, assistant }: { draft?: { text: string }; assistant?: boolean
    suggestions?: { items: Array<{ label: string }> } }) =>
    <div data-testid="composer" data-assistant={String(assistant)}>
      <output>{draft?.text}</output>{suggestions?.items.map((item) => <span key={item.label}>{item.label}</span>)}
    </div> }))
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
let pendingQuestions: QuestionRequest[]
let client: QueryClient
/** What the server files in the main session before the assistant writes
 *  into a workspace-visible conversation (assistant/confirmations.py). */
const confirmation: QuestionRequest = { id: "confirm-send", session_id: "main", status: "pending",
  tool: { callID: "followup-call", messageID: "reply" },
  questions: [{ header: "确认发送", custom: false,
    question: "把下面这段话发送到工作区可见的会话「贪吃蛇」吗？工作区成员都能看到这条消息。\n\n把主题改成暗色",
    options: [{ label: "确认发送", description: "由个人助理代你发送这段话" }, { label: "取消", description: "不发送" }] }] }

function route(url: string) {
  if (url === "/api/assistant?answer_scope=unread") return snapshot
  if (url.startsWith("/api/assistant/events?")) return { state: "ready", assistant_session_id: "main", next_cursor: "cursor",
    next_sequence: 0, events: [], has_more: false }
  if (url.startsWith("/api/assistant/requests?")) return { items: [], next_cursor: null, receipts: [] }
  if (url === "/api/agent/session/main") return session
  if (url.startsWith("/api/agent/session/main/history?")) return { messages: history, has_more: false }
  if (url === "/api/agent/question") return pendingQuestions
  if (url === "/api/agent/permission" || url === "/api/agent/agent") return []
  if (url === "/api/agent/config") return {}
  if (url === "/api/assistant/requests/waiting") return { items: [] }
  if (url === "/api/assistant/watch") return { items: [], has_more: false }
  throw new Error(`Unexpected read: ${url}`)
}

beforeEach(() => {
  listeners.clear()
  history = [user("question", 1, text("question-text", "Summarize my day", { origin: "human" }))]
  pendingQuestions = []
  usePendingStore.getState().reset()
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

it.each(["socket", "list"])("renders a pending main-session confirmation (%s) as an answerable card", async (arrival) => {
  if (arrival === "list") pendingQuestions = [confirmation]
  vi.mocked(http.post).mockResolvedValue({ ok: true, session_id: "main" })
  mount()
  await screen.findByText("Summarize my day")
  if (arrival === "socket") emit("question.asked", confirmation as unknown as Frame)
  expect(await screen.findByText(/把主题改成暗色/)).toBeTruthy()
  const send = screen.getByRole("button", { name: "确认发送" })
  expect(screen.getByRole("button", { name: "取消" })).toBeTruthy()
  // custom=false: the card offers exactly the two choices, no free-text field.
  expect(screen.queryByRole("textbox", { name: /把主题改成暗色/ })).toBeNull()
  fireEvent.click(send)
  fireEvent.click(screen.getByTestId("question-primary-action"))
  await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/agent/question/confirm-send", { answers: [["确认发送"]] }))
  await waitFor(() => expect(screen.queryByText(/把主题改成暗色/)).toBeNull())
})

it("welcomes a first-time user and puts a chosen idea into the composer", async () => {
  history = []
  mount()
  expect(await screen.findByText("assistant.welcome.intro")).toBeTruthy()
  fireEvent.click(screen.getByText("assistant.welcome.ideas.progress.title"))
  await waitFor(() => expect(screen.getByTestId("composer").textContent).toContain("assistant.welcome.ideas.progress.prompt"))
  expect(screen.getByTestId("composer").getAttribute("data-assistant")).toBe("true")
})

it("speaks as the assistant and offers quick follow-ups once the conversation is quiet", async () => {
  history = [...history, reply("answer", 2, "Three meetings today.")]
  mount()
  expect(await screen.findByText("Three meetings today.")).toBeTruthy()
  expect(screen.getByText("assistant.name")).toBeTruthy()
  expect(screen.queryByText("assistant.welcome.intro")).toBeNull()
  await waitFor(() => expect(screen.getByText("assistant.quick.progress.label")).toBeTruthy())
  expect(screen.getByText("assistant.quick.waiting.label")).toBeTruthy()
})

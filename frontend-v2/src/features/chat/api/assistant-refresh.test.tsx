import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { useChatEvents } from "../hooks/useChatEvents"
import { useChatHistory } from "../hooks/useChatHistory"
import { useStreamStore } from "../stores/stream"
import { assistantKeys, useAssistantEvents, type AssistantSnapshot } from "./assistant"

type Frame = { sessionId: string; generation?: number; [key: string]: unknown }
const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<(data: Frame) => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { connect: vi.fn(), on: (event: string, callback: (data: Frame) => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() },
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (value: string) => value }) }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const question: MessageWithParts = { id: "question", session_id: "main", role: "user", created_at: "2026-10-06T00:00:00Z",
  parts: [{ id: "question-text", type: "text", text: "What changed?", origin: "human" }] }
const initial = { state: "ready", session: { id: "main" }, event_cursor: "initial", high_water_mark: 0,
  last_seen_sequence: 0, tasks: [], answers: [] } as unknown as AssistantSnapshot
const page = (changed = false) => ({ state: "ready", assistant_session_id: "main", next_cursor: "next", next_sequence: 1,
  events: changed ? [{ event_id: "event", sequence: 1, kind: "assistant.task.changed" }] : [], has_more: false })
let client: QueryClient
let changed: boolean

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  listeners.clear()
  changed = false
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  useStreamStore.setState({ messages: new Map(), history: new Map(), status: new Map(), statusGeneration: new Map(),
    terminalStatusGeneration: new Map() })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(assistantKeys.snapshot("owner", "workspace"), initial)
  vi.mocked(http.get).mockImplementation(async (url) => {
    if (url.startsWith("/api/assistant/events")) return page(changed)
    if (url.startsWith("/api/agent/session/main/history?")) return { messages: [question], has_more: false }
    throw new Error(`Unexpected read: ${url}`)
  })
})
afterEach(() => { cleanup(); client.clear(); vi.useRealTimers() })
const tick = (ms = 200) => act(() => vi.advanceTimersByTimeAsync(ms))
const emit = (event: string, data: Frame = { sessionId: "main", generation: 1 }) => act(() => {
  listeners.get(event)?.forEach((callback) => callback(data))
})
const calls = (part: string) => vi.mocked(http.get).mock.calls.filter(([url]) => url.includes(part))
const held = () => useStreamStore.getState().messages.get("main") ?? []

/** The assistant page mounts durable replay beside the ordinary chat bridge. */
async function mount(surface: "assistant" | "workspace" = "assistant") {
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  renderHook(() => {
    useAssistantEvents(surface === "assistant" ? "main" : undefined)
    useChatEvents("main", surface)
    useChatHistory("main", false)
  }, { wrapper })
  await tick()
  expect(held().map((message) => message.id)).toEqual(["question"])
  vi.mocked(http.get).mockClear()
}

it("applies streamed message, part and tool frames to the main session without a history refresh", async () => {
  await mount()
  emit("message.created", { sessionId: "main", generation: 1, message: { id: "reply", session_id: "main", role: "assistant",
    created_at: "2026-10-06T00:00:01Z", parts: [{ id: "reply-text", type: "text", text: "" }] } })
  emit("message.text_delta", { sessionId: "main", generation: 1, messageId: "reply", partId: "reply-text", text: "Two files " })
  emit("part.delta", { sessionId: "main", generation: 1, messageId: "reply", partId: "reply-text", delta: "changed." })
  emit("part.created", { sessionId: "main", generation: 1, messageId: "reply",
    part: { id: "reply-tool", type: "tool", tool: "read", status: "running" } })
  emit("tool.completed", { sessionId: "main", generation: 1, partId: "reply-tool", data: { output: "diff" } })
  expect(held().map((message) => message.id)).toEqual(["question", "reply"])
  expect(held()[1].parts).toEqual([
    { id: "reply-text", type: "text", text: "Two files changed." },
    { id: "reply-tool", type: "tool", tool: "read", status: "completed", output: "diff" },
  ])
  await tick()
  // Applying a frame needs no read. The tool frame only nudges durable replay.
  expect(calls("/history?")).toHaveLength(0)
  expect(calls("/api/assistant/messages")).toHaveLength(0)
  expect(calls("/api/assistant/events")).toHaveLength(1)
})

it("pulls the final history once when a main run settles, like any chat", async () => {
  await mount()
  emit("session.status", { sessionId: "main", generation: 1, status: "busy" })
  await tick()
  expect(calls("/history?")).toHaveLength(0)
  emit("session.status", { sessionId: "main", generation: 1, status: "idle" })
  await tick()
  expect(calls("/history?")).toHaveLength(1)
})

it("refreshes assistant task views from durable events but never the transcript, polling every 15 seconds", async () => {
  await mount()
  changed = true
  const taskKey = assistantKeys.task("owner", "workspace", "task")
  client.setQueryData(taskKey, { stale: true })
  await tick(5_000)
  expect(calls("/api/assistant/events")).toHaveLength(0)
  await tick(10_000)
  expect(calls("/api/assistant/events")).toHaveLength(1)
  expect(client.getQueryState(taskKey)!.isInvalidated).toBe(true)
  expect(calls("/history?")).toHaveLength(0)
})

it("catches the main transcript up once on reconnect and replays durable events", async () => {
  await mount()
  emit("__connected")
  await tick()
  expect(calls("/history?")).toHaveLength(1)
  expect(calls("/api/assistant/events")).toHaveLength(1)
  expect(calls("/api/assistant/messages")).toHaveLength(0)
})

it("waits for a new hint or the poll after an events read error instead of spinning", async () => {
  await mount()
  vi.mocked(http.get).mockImplementation(async () => { throw new Error("Read unavailable") })
  emit("session.status", { sessionId: "main", generation: 1, status: "busy" })
  await tick()
  expect(calls("/api/assistant/events")).toHaveLength(1)
  await tick(1_000)
  expect(calls("/api/assistant/events")).toHaveLength(1)
  emit("session.status", { sessionId: "main", generation: 1, status: "busy" })
  await tick()
  expect(calls("/api/assistant/events")).toHaveLength(2)
})

it("ignores status frames from an older generation", async () => {
  await mount()
  useStreamStore.getState().applyStatusEvent("main", "busy", 2)
  emit("session.status", { sessionId: "main", generation: 1, status: "idle" })
  await tick()
  expect(http.get).not.toHaveBeenCalled()
})

it("retains ordinary conversation streaming, history invalidation and reconnect catch-up", async () => {
  await mount("workspace")
  emit("message.created", { sessionId: "main", generation: 1,
    message: { id: "new-message", session_id: "main", role: "user", created_at: "2026-10-06T00:00:02Z", parts: [] } })
  expect(held().at(-1)?.id).toBe("new-message")
  // Transcript membership changed, but the ordinary live bridge needs no
  // history request just to apply a streamed message.
  expect(calls("/history?")).toHaveLength(0)
  emit("__connected")
  await tick()
  expect(calls("/history?")).toHaveLength(1)
  expect(calls("/api/assistant/")).toHaveLength(0)
})

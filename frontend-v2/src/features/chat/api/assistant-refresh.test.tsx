import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { useChatEvents } from "../hooks/useChatEvents"
import { useStreamStore } from "../stores/stream"
import { assistantKeys, useAssistantEvents, type AssistantSnapshot } from "./assistant"
import { useAssistantTranscript } from "./assistant-transcript"
import { useMessagesQuery } from "./messages"

type Hint = { sessionId: string; generation?: number; [key: string]: unknown }
const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<(data: Hint) => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { connect: vi.fn(), on: (event: string, callback: (data: Hint) => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() },
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (value: string) => value }) }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const message = (status: "available" | "unavailable"): MessageWithParts => ({
  id: "answer", session_id: "main", role: "assistant", created_at: "2026-10-05T00:00:00Z", parts: [],
  source_status: status, source_checked_at: status === "available" ? "2026-10-05T00:00:00Z" : "2026-10-05T00:00:01Z",
})
const initial = { state: "ready", session: { id: "main" }, event_cursor: "initial", high_water_mark: 0,
  last_seen_sequence: 0, tasks: [], answers: [] } as unknown as AssistantSnapshot
const page = (changed = false) => ({ state: "ready", assistant_session_id: "main", next_cursor: "next", next_sequence: 1,
  events: changed ? [{ event_id: "event", sequence: 1, kind: "assistant.message.committed" }] : [], has_more: false })
let client: QueryClient
let changed: boolean
let status: "available" | "unavailable"
let delay: boolean
let pending: Array<{ signal?: AbortSignal | null; finish: () => void }>

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  listeners.clear()
  changed = false; status = "available"; delay = false; pending = []
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  useStreamStore.setState({ messages: new Map([["main", [message("available")]]]), statusGeneration: new Map(),
    terminalStatusGeneration: new Map() })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(assistantKeys.snapshot("owner", "workspace"), initial)
  vi.mocked(http.get).mockImplementation(async (url, options) => {
    if (url.startsWith("/api/assistant/events")) return page(changed)
    const value = { messages: [message(status)], has_more: false }
    if (!delay) return value
    return new Promise((resolve) => { pending.push({ signal: options?.signal, finish: () => resolve(value) }) })
  })
})
afterEach(() => { cleanup(); client.clear(); vi.useRealTimers() })
const tick = (ms = 200) => act(() => vi.advanceTimersByTimeAsync(ms))
const emit = (event: string, data: Hint = { sessionId: "main", generation: 1 }) => act(() => {
  listeners.get(event)?.forEach((callback) => callback(data))
})
const calls = (part: string) => vi.mocked(http.get).mock.calls.filter(([url]) => url.includes(part))
function mount(surface: "assistant" | "workspace" = "assistant") {
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  return renderHook(() => {
    useAssistantEvents(surface === "assistant" ? "main" : undefined)
    useChatEvents("main", surface)
    return { history: useMessagesQuery("main"), transcript: useAssistantTranscript("main") }
  }, { wrapper })
}

it("coalesces duplicate history hints and durable events, then rechecks after an in-flight change", async () => {
  const view = mount()
  await tick()
  vi.mocked(http.get).mockClear()
  changed = true; delay = true
  for (let index = 0; index < 10; index++) emit("assistant.history.changed")
  await tick()
  expect(calls("/history?")).toHaveLength(1)
  expect(calls("/api/assistant/messages?")).toHaveLength(1)
  for (let index = 0; index < 20; index++) emit("assistant.history.changed")
  expect(pending).toHaveLength(2)
  expect(pending.some((request) => request.signal?.aborted)).toBe(false)
  status = "unavailable"; changed = false; delay = false
  await act(async () => { pending.forEach((request) => request.finish()) })
  await tick()
  expect(calls("/history?")).toHaveLength(2)
  expect(calls("/api/assistant/messages?")).toHaveLength(2)
  expect(view.result.current.history.data?.messages[0].source_status).toBe("unavailable")
  expect(view.result.current.transcript.messages[0].source_status).toBe("unavailable")
})

it("refreshes retained source checks on reconnect even without a new durable event", async () => {
  const view = mount()
  await tick()
  vi.mocked(http.get).mockClear()
  status = "unavailable"
  emit("__connected")
  await tick()
  expect(calls("/history?")).toHaveLength(1)
  expect(calls("/api/assistant/messages?")).toHaveLength(1)
  expect(view.result.current.transcript.messages[0].source_status).toBe("unavailable")
})

it("ignores a history hint from an older generation", async () => {
  mount()
  await tick()
  useStreamStore.getState().applyStatusEvent("main", "busy", 2)
  vi.mocked(http.get).mockClear()
  emit("assistant.history.changed", { sessionId: "main", generation: 1 })
  await tick()
  expect(http.get).not.toHaveBeenCalled()
})

it("retains ordinary conversation streaming, history invalidation and reconnect catch-up", async () => {
  const view = mount("workspace")
  await tick()
  vi.mocked(http.get).mockClear()
  emit("message.created", { sessionId: "main", generation: 1,
    message: { id: "new-message", session_id: "main", role: "user", created_at: "2026-10-05T00:00:02Z", parts: [] } })
  expect(useStreamStore.getState().messages.get("main")?.at(-1)?.id).toBe("new-message")
  // Transcript membership changed, but the ordinary live bridge needs no
  // history request just to apply a streamed message.
  expect(calls("/history?")).toHaveLength(0)
  emit("assistant.history.changed")
  await tick()
  expect(calls("/history?")).toHaveLength(1)
  status = "unavailable"
  emit("__connected")
  await tick()
  expect(calls("/history?")).toHaveLength(2)
  expect(view.result.current.history.data?.messages[0].source_status).toBe("unavailable")
})

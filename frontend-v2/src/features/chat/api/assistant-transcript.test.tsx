import type { ReactNode } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { useStreamStore } from "../stores/stream"
import { assistantKeys } from "./assistant"
import { refreshTranscriptPages, useAssistantTranscript } from "./assistant-transcript"

vi.mock("@/shared/api/http", async (original) => ({ ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() } }))
const row = (id: string): MessageWithParts => ({ id, session_id: "s", role: "assistant", created_at: "", parts: [],
  source_status: "available", source_checked_at: "2026-10-03T10:00:01.000000+00:00" })
beforeEach(() => {
  vi.clearAllMocks()
  useAuthStore.setState({ user: { id: "actor", username: "actor", role: "user" } })
  useWorkspaceStore.setState({ currentId: "workspace" })
  useStreamStore.getState().clearMessages("s")
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
function mount(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  return { ...renderHook(() => useAssistantTranscript("s"), { wrapper }), client }
}

describe("assistant transcript source checks", () => {
  it("checks all loaded pages in bounded actor/workspace-scoped batches without rewriting the stream", async () => {
    const loaded = Array.from({ length: 205 }, (_, n) => row(`message-${n}`))
    useStreamStore.getState().setMessages("s", loaded)
    const held = useStreamStore.getState().messages.get("s")
    vi.mocked(http.get).mockImplementation(async (url) => ({ messages:
      new URL(String(url), "http://test").searchParams.getAll("message_ids").map(row),
    }))
    const { result, client } = mount()
    await waitFor(() => expect(result.current.messages).toHaveLength(205))
    expect(http.get).toHaveBeenCalledTimes(3)
    for (const [url, options] of vi.mocked(http.get).mock.calls) {
      expect(new URL(String(url), "http://test").searchParams.getAll("message_ids").length).toBeLessThanOrEqual(100)
      expect(options?.headers).toEqual({ "X-Workspace-Id": "workspace" })
    }
    expect(client.getQueryCache().getAll().every((query) => query.queryKey.slice(0, 3).join() === "assistant,actor,workspace")).toBe(true)
    expect(useStreamStore.getState().messages.get("s")).toBe(held)
  })
  it("does not restore content when a poll started before the click-time revocation check returns late", async () => {
    const available = row("answer")
    useStreamStore.getState().setMessages("s", [available])
    vi.mocked(http.get).mockResolvedValue({ messages: [available] })
    const { result, client } = mount()
    await waitFor(() => expect(result.current.messages).toHaveLength(1))
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const revoked = { ...available, source_status: "unavailable" as const, source_checked_at: "2026-10-03T10:00:02.000000+00:00" }
    await act(async () => {
      const poll = client.refetchQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s") })
      refreshTranscriptPages(client, assistantKeys.transcripts("actor", "workspace", "s"), { messages: [revoked] })
      finish({ messages: [available] })
      await poll
    })
    await waitFor(() => expect(result.current.messages).toEqual([revoked]))
  })
  it("requires fresh proof on remount even when every page was retained in Query", async () => {
    const message = row("answer")
    useStreamStore.getState().setMessages("s", [message])
    vi.mocked(http.get).mockResolvedValue({ messages: [message] })
    const initial = mount()
    await waitFor(() => expect(initial.result.current.pending).toBe(false))
    initial.unmount()
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const returned = mount(initial.client)
    expect(returned.result.current.pending).toBe(true)
    await act(async () => finish({ messages: [{ ...message, source_status: "unavailable", parts: [] }] }))
    await waitFor(() => expect(returned.result.current.pending).toBe(false))
    expect(returned.result.current.messages[0].source_status).toBe("unavailable")
  })
  it("hides held sources after a validation failure or incomplete server projection", async () => {
    useStreamStore.getState().setMessages("s", [row("a"), row("b")])
    vi.mocked(http.get).mockResolvedValue({ messages: [row("a")] })
    const { result } = mount()
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.messages).toEqual([])
  })
  it("rechecks all retained pages on the periodic interval", async () => {
    vi.useFakeTimers()
    const loaded = Array.from({ length: 205 }, (_, n) => row(`m${n}`))
    useStreamStore.getState().setMessages("s", loaded)
    let revoked = false
    vi.mocked(http.get).mockImplementation(async (url) => ({ messages:
      new URL(String(url), "http://test").searchParams.getAll("message_ids").map((id) => ({...row(id),
        source_status: revoked ? "unavailable" : "available", source_checked_at: revoked ? "2026-10-03T10:00:02Z" : row(id).source_checked_at })),
    }))
    const { result } = mount()
    await act(() => vi.advanceTimersByTimeAsync(100))
    expect(result.current.messages).toHaveLength(205)
    revoked = true
    await act(() => vi.advanceTimersByTimeAsync(15_100))
    expect(http.get).toHaveBeenCalledTimes(6)
    expect(result.current.messages.every((m) => m.source_status === "unavailable")).toBe(true)
  })
})

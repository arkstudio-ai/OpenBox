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
import { fetchHistory, useLiveHistory, useMessagesQuery } from "./messages"
import { chatKeys } from "./keys"
import { createHistoryProofReader, requireFreshHistoryProof } from "./history-source-proof"
import { sourceProjection } from "../lib/source-projection"

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
  it.each(["newest", "live"])("keeps fresh HTTP proof through a cached %s history Query", async (kind) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const key = kind === "newest" ? chatKeys.messages("actor", "s") : chatKeys.liveHistory("actor", "s")
    client.setQueryData(key, { messages: [row("answer")], has_more: false, ...(kind === "live" ? { windowed: true } : {}) })
    const message = { ...row("answer"), source_checked_at: "2026-10-03T10:00:02.000000+00:00" }
    vi.mocked(http.get).mockResolvedValue({ messages: [message], has_more: false })
    const since = Date.now()
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const useHistory = kind === "newest" ? () => useMessagesQuery("s") : () => useLiveHistory("s", true)
    const { result } = renderHook(useHistory, { wrapper })
    await waitFor(() => expect(result.current.data?.messages[0].source_checked_at).toBe(message.source_checked_at))
    const readProof = createHistoryProofReader()
    expect(readProof(result.current.data!.messages, { userId: "actor", workspaceId: "workspace" }, since)).toEqual([message])
  })
  it("shows a newly verified history response without a second request, then polls fresh sources", async () => {
    const message = row("answer")
    vi.mocked(http.get).mockResolvedValue({ messages: [message], has_more: false })
    const { result, client } = mount()
    const page = await fetchHistory("s")
    await act(async () => useStreamStore.getState().setMessages("s", page.messages))
    await waitFor(() => expect(result.current.pending).toBe(false))
    expect(result.current.messages).toEqual([message])
    expect(http.get).toHaveBeenCalledTimes(1)
    expect(vi.mocked(http.get).mock.calls[0][0]).toContain("/history?")
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...message, source_status: "unavailable", parts: [] }] })
    await act(async () => { await client.refetchQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s"), type: "active" }) })
    expect(http.get).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(result.current.messages[0].source_status).toBe("unavailable"))
  })
  it("does not reuse a history response from another workspace", async () => {
    const message = row("answer")
    vi.mocked(http.get).mockResolvedValue({ messages: [message], has_more: false })
    const page = await fetchHistory("s")
    useStreamStore.getState().setMessages("s", page.messages)
    useWorkspaceStore.setState({ currentId: "other-workspace" })
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const { result } = mount()
    expect(result.current.pending).toBe(true)
    await waitFor(() => expect(http.get).toHaveBeenCalledTimes(2))
    expect(vi.mocked(http.get).mock.calls[1][1]?.headers).toEqual({ "X-Workspace-Id": "other-workspace" })
    await act(async () => finish({ messages: [{ ...message, source_status: "unavailable" }] }))
    await waitFor(() => expect(result.current.pending).toBe(false))
    expect(result.current.messages[0].source_status).toBe("unavailable")
  })
  it("merges a new history page and rechecks only the already consumed subset", async () => {
    const { result, client } = mount()
    vi.mocked(http.get).mockResolvedValue({ messages: [row("newer")], has_more: true })
    const first = await fetchHistory("s")
    await act(async () => useStreamStore.getState().setMessages("s", first.messages))
    await waitFor(() => expect(result.current.pending).toBe(false))
    vi.mocked(http.get).mockImplementation(async (url) => String(url).includes("/history?")
      ? { messages: [row("older")], has_more: false }
      : { messages: new URL(String(url), "http://test").searchParams.getAll("message_ids").map(row) })
    const second = await fetchHistory("s", { before: "newer" })
    await act(async () => useStreamStore.getState().setMessages("s", [...second.messages, ...first.messages]))
    await waitFor(() => expect(result.current.pending).toBe(false))
    expect(result.current.messages.map((message) => message.id)).toEqual(["older", "newer"])
    expect(http.get).toHaveBeenCalledTimes(3)
    expect(new URL(vi.mocked(http.get).mock.calls[2][0], "http://test").searchParams.getAll("message_ids")).toEqual(["newer"])
    vi.mocked(http.get).mockClear()
    vi.mocked(http.get).mockImplementation(async (url) => ({ messages:
      new URL(String(url), "http://test").searchParams.getAll("message_ids").map((id) => ({ ...row(id), source_status: "unavailable" })),
    }))
    await act(async () => { await client.refetchQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s"), type: "active" }) })
    await waitFor(() => expect(result.current.messages.every((message) => message.source_status === "unavailable")).toBe(true))
    expect(http.get).toHaveBeenCalledTimes(1)
    expect(new URL(vi.mocked(http.get).mock.calls[0][0], "http://test").searchParams.getAll("message_ids")).toEqual(["older", "newer"])
  })
  it("reuses real 32/23-message history responses together once without an extra read", async () => {
    const { result, client } = mount()
    vi.mocked(http.get).mockResolvedValueOnce({ messages: Array.from({ length: 32 }, (_, n) => row(`new-${n}`)), has_more: true })
    const newer = await fetchHistory("s")
    vi.mocked(http.get).mockResolvedValueOnce({ messages: Array.from({ length: 23 }, (_, n) => row(`old-${n}`)), has_more: false })
    const older = await fetchHistory("s", { before: "new-0" })
    const messages = [...older.messages, ...newer.messages]
    await act(async () => useStreamStore.getState().setMessages("s", messages))
    await waitFor(() => expect(result.current.pending).toBe(false))
    expect(result.current.messages).toEqual(messages)
    expect(http.get).toHaveBeenCalledTimes(2)
    expect(client.getQueryCache().getAll()).toHaveLength(1)
    vi.mocked(http.get).mockImplementation(async (url) => ({ messages:
      new URL(url, "http://test").searchParams.getAll("message_ids").map(row).reverse(),
    }))
    await act(async () => { await client.refetchQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s"), type: "active" }) })
    expect(http.get).toHaveBeenCalledTimes(3)
    expect(result.current.messages.map((message) => message.id)).toEqual(messages.map((message) => message.id))
  })
  it.each(["failed", "canceled"])("consumes partial history once when its missing-subset read is %s", async (outcome) => {
    const { result, client } = mount()
    vi.mocked(http.get).mockResolvedValueOnce({ messages: [row("new")], has_more: false })
    const page = await fetchHistory("s")
    let finish!: (value: unknown) => void, fail!: (reason: Error) => void
    let signal: AbortSignal | null | undefined
    vi.mocked(http.get).mockImplementationOnce((_, options) => {
      signal = options?.signal
      return new Promise((resolve, reject) => { finish = resolve; fail = reject })
    })
    const loaded = [row("old"), ...page.messages]
    await act(async () => useStreamStore.getState().setMessages("s", loaded))
    expect(new URL(vi.mocked(http.get).mock.calls[1][0], "http://test").searchParams.getAll("message_ids")).toEqual(["old"])
    if (outcome === "failed") {
      await act(async () => fail(new Error("Unavailable")))
      await waitFor(() => expect(result.current.failed).toBe(true))
      expect([...result.current.unavailableIds]).toEqual(["old", "new"])
    } else {
      await act(async () => {
        await client.cancelQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s") })
        finish({ messages: [row("old")] })
      })
      expect(signal?.aborted).toBe(true)
      expect(result.current.pending).toBe(true)
    }
    expect(result.current.messages).toEqual([])
    vi.mocked(http.get).mockImplementation(async (url) => ({ messages:
      new URL(url, "http://test").searchParams.getAll("message_ids").map((id) => ({ ...row(id), source_status: "unavailable" })),
    }))
    await act(async () => { await client.refetchQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s"), type: "active" }) })
    expect(new URL(vi.mocked(http.get).mock.calls[2][0], "http://test").searchParams.getAll("message_ids")).toEqual(["old", "new"])
    await waitFor(() => expect(result.current.messages.every((message) => message.source_status === "unavailable")).toBe(true))
  })
  it("rejects a partial projection when a source hint arrives during its missing-subset read", async () => {
    const { result, client } = mount()
    vi.mocked(http.get).mockResolvedValueOnce({ messages: [row("new")], has_more: false })
    const page = await fetchHistory("s")
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    await act(async () => useStreamStore.getState().setMessages("s", [row("old"), ...page.messages]))
    requireFreshHistoryProof(client, { userId: "actor", workspaceId: "workspace" }, "s")
    await act(async () => finish({ messages: [row("old")] }))
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.messages).toEqual([])
    expect([...result.current.unavailableIds]).toEqual(["old", "new"])
  })
  it("does not reuse a history response begun before a same-millisecond source hint", async () => {
    vi.spyOn(Date, "now").mockReturnValue(Date.now())
    const { result, client } = mount()
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const pending = fetchHistory("s")
    requireFreshHistoryProof(client, { userId: "actor", workspaceId: "workspace" }, "s")
    finish({ messages: [row("answer")], has_more: false })
    const page = await pending
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...row("answer"), source_status: "unavailable" }] })
    await act(async () => useStreamStore.getState().setMessages("s", page.messages))
    await waitFor(() => expect(result.current.pending).toBe(false))
    expect(http.get).toHaveBeenCalledTimes(2)
    expect(result.current.messages[0].source_status).toBe("unavailable")
  })
  it("rejects a pending source read after hiding and returning to the foreground", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
    useStreamStore.getState().setMessages("s", [row("answer")])
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const { result, client } = mount()
    await act(async () => {
      visibility.mockReturnValue("hidden")
      document.dispatchEvent(new Event("visibilitychange"))
    })
    expect(result.current.scopePending).toBe(true)
    await act(async () => {
      visibility.mockReturnValue("visible")
      document.dispatchEvent(new Event("visibilitychange"))
      finish({ messages: [row("answer")] })
    })
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.messages).toEqual([])
    expect([...result.current.unavailableIds]).toEqual(["answer"])
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...row("answer"), source_status: "unavailable" }] })
    await act(async () => { await client.refetchQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s"), type: "active" }) })
    await waitFor(() => expect(result.current.messages[0].source_status).toBe("unavailable"))
  })
  it("revalidates a previously fetched history response on a later mount", async () => {
    let now = Date.now()
    vi.spyOn(Date, "now").mockImplementation(() => now)
    vi.mocked(http.get).mockResolvedValue({ messages: [row("answer")], has_more: false })
    const page = await fetchHistory("s")
    useStreamStore.getState().setMessages("s", page.messages)
    now += 100
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...row("answer"), source_status: "unavailable" }] })
    const { result } = mount()
    await waitFor(() => expect(result.current.pending).toBe(false))
    expect(http.get).toHaveBeenCalledTimes(2)
    expect(result.current.messages[0].source_status).toBe("unavailable")
  })
  it("renders a verified page while another page is pending or fails", async () => {
    const loaded = Array.from({ length: 101 }, (_, n) => row(`message-${n}`))
    useStreamStore.getState().setMessages("s", loaded)
    let fail!: (error: Error) => void
    vi.mocked(http.get).mockImplementation((url) => {
      const ids = new URL(String(url), "http://test").searchParams.getAll("message_ids")
      return ids.length === 1 ? new Promise((_, reject) => { fail = reject }) : Promise.resolve({ messages: ids.map(row) })
    })
    const { result } = mount()
    await waitFor(() => expect(result.current.messages).toHaveLength(100))
    const project = (index: number) => sourceProjection(loaded[index], {
      transcript: new Map(result.current.messages.map((message) => [message.id, message])),
      sourcesPending: result.current.scopePending, pendingIds: result.current.pendingIds,
      unavailableIds: result.current.unavailableIds,
    })
    expect(project(0).source_status).toBe("available")
    expect(project(100).source_status).toBe("pending")
    await act(async () => fail(new Error("Second page unavailable")))
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(project(0).source_status).toBe("available")
    expect(project(100).source_status).toBe("unavailable")
  })
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
      const poll = client.refetchQueries({ queryKey: assistantKeys.transcripts("actor", "workspace", "s"), type: "active" })
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

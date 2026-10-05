import { useContext, useLayoutEffect } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { AssistantReadBoundary, ExecutionReadBoundary, VisibleAssistantAnswer } from "../components/AssistantReadBoundary"
import { AssistantReadContext } from "../hooks/assistant-read-context"
import { useVerifiedAssistantCopy } from "../hooks/useVerifiedAssistantCopy"
import { sourceProjection } from "../lib/source-projection"
import { useStreamStore } from "../stores/stream"
import { assistantKeys, useAssistantEvents, type AssistantSnapshot } from "./assistant"
import { refreshTranscriptPages, type TranscriptPage } from "./assistant-transcript"
import { historyProofBarrier, requireFreshHistoryProof } from "./history-source-proof"

const { copy, mutate } = vi.hoisted(() => ({ copy: vi.fn(), mutate: vi.fn() }))
type Hint = { sessionId: string; generation?: number }
const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<(data: Hint) => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { on: (event: string, callback: (data: Hint) => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("@/shared/api/http", async (original) => ({ ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() } }))
vi.mock("./assistant", async (original) => ({ ...await original<typeof import("./assistant")>(), useAssistantReadCursor: () => ({ mutate }) }))
vi.mock("@/shared/hooks/useCopy", () => ({ useCopy: () => ({ copy, copied: false }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Read failed" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

const at = (n: number) => `2026-10-06T01:00:00.${String(n).padStart(6, "0")}+00:00`
const row = (id: string, sessionId = "s", checkedAt = at(1)): MessageWithParts => ({
  id, session_id: sessionId, role: "assistant", created_at: "", finish: "stop", source_status: "available", source_checked_at: checkedAt,
  parts: [{ id: `part-${id}`, type: "text", channel: "final", text: `body:${id}` }],
})
const rows = (count: number, sessionId = "s") => Array.from({ length: count }, (_, n) => row(`m${n}`, sessionId))
function pending() {
  let resolve!: (value: TranscriptPage) => void, reject!: (error: Error) => void
  const promise = new Promise<TranscriptPage>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const ids = (url: string) => new URL(url, "http://test").searchParams.getAll("message_ids")
function respond() {
  vi.mocked(http.get).mockImplementation(async (url) => {
    if (url.startsWith("/api/assistant/events")) return { state: "ready", assistant_session_id: "s",
      next_cursor: "next", next_sequence: 0, events: [], has_more: false }
    const sessionId = new URL(url, "http://test").searchParams.get("session_id")!
    return { messages: ids(url).map((id) => row(id, sessionId)) }
  })
}
function state(answers: AssistantSnapshot["answers"] = [], checkedAt = at(2)): AssistantSnapshot {
  return { state: "ready", session: { id: "s", user_id: "actor", workspace_id: "workspace" },
    answers, last_seen_sequence: 0, source_checked_at: checkedAt } as AssistantSnapshot
}
function ProjectedRows({ sessionId }: { sessionId: string }) {
  const context = useContext(AssistantReadContext)
  const messages = useStreamStore((store) => store.messages.get(sessionId) ?? [])
  return <>{messages.map((message) => {
    const projected = sourceProjection(message, context)
    return <div key={message.id} data-testid={message.id} data-status={projected.source_status}
      data-checked-at={context?.transcript?.get(message.id)?.source_checked_at ?? ""}>
      {projected.source_status === "available" ? <VisibleAssistantAnswer messageId={message.id}>
        {projected.parts.filter((part) => part.type === "text").map((part) => part.text).join("")}
      </VisibleAssistantAnswer> : projected.source_status}
    </div>
  })}</>
}
function CopyAction() {
  const { copyReply } = useVerifiedAssistantCopy("s", "m0", "body:m0")
  return <button onClick={() => { void copyReply() }}>Copy verified</button>
}
function EventRefresh() { useAssistantEvents("s"); return null }
function CopyDuringCommit({ client }: { client: QueryClient }) {
  const count = useStreamStore((store) => store.messages.get("s")?.length ?? 0)
  useLayoutEffect(() => {
    if (count !== 3) return
    // Start the actual registered Query before changing A. The replacement
    // must have begun under the old epoch, not merely return an old fixture
    // after a genuinely later request started.
    const key = assistantKeys.transcript("actor", "workspace", "s", ["m0", "m1", "m2"])
    const registered = client.getQueryCache().find({ queryKey: key, exact: true })
    if (!registered?.options.queryFn) throw new Error("Expected the real replacement query function")
    // refetchQueries deliberately excludes not-yet-observed empty queries.
    // fetchQuery runs the hook's actual registered function at this boundary.
    void client.fetchQuery({ ...registered.options, queryKey: key }).catch(() => {})
    if (client.getQueryState(key)?.fetchStatus !== "fetching") throw new Error("Replacement must be in flight before the copy")
    refreshTranscriptPages(client, assistantKeys.transcripts("actor", "workspace", "s"), {
      messages: [{ ...row("m0"), source_status: "unavailable", source_checked_at: at(10), parts: [] }],
    })
  }, [client, count])
  return null
}
const clients: QueryClient[] = []
const observers: Observer[] = []
class Observer {
  observe = vi.fn()
  disconnect = vi.fn()
  constructor(readonly callback: IntersectionObserverCallback) { observers.push(this) }
  show() { this.callback([{ isIntersecting: true, intersectionRect: { height: 20, width: 100 } } as IntersectionObserverEntry], this as unknown as IntersectionObserver) }
}
beforeEach(() => {
  vi.clearAllMocks()
  listeners.clear()
  useAuthStore.setState({ user: { id: "actor", username: "actor", role: "user" } })
  useWorkspaceStore.setState({ currentId: "workspace" })
  useStreamStore.getState().clearMessages("s")
  useStreamStore.getState().clearMessages("other")
  observers.length = 0
  vi.stubGlobal("IntersectionObserver", Observer)
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
  respond()
})
afterEach(() => { cleanup(); for (const client of clients.splice(0)) client.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function mount(initial?: AssistantSnapshot, events = false, duringCommit = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  if (events) client.setQueryData(assistantKeys.snapshot("actor", "workspace"), { ...state(), event_cursor: "initial", high_water_mark: 0 })
  const tree = (snapshot = initial, sessionId = "s") => <QueryClientProvider client={client}>
    {events && <EventRefresh />}
    {snapshot ? <AssistantReadBoundary snapshot={snapshot}><ProjectedRows sessionId={sessionId} /><CopyAction /></AssistantReadBoundary>
      : <ExecutionReadBoundary sessionId={sessionId}><ProjectedRows sessionId={sessionId} />{duringCommit && <CopyDuringCommit client={client} />}</ExecutionReadBoundary>}
  </QueryClientProvider>
  const view = render(tree())
  return { ...view, client, update: (snapshot?: AssistantSnapshot, sessionId = "s") => view.rerender(tree(snapshot, sessionId)) }
}
const status = (id: string) => screen.getByTestId(id).getAttribute("data-status")
const prefix = () => assistantKeys.transcripts(useAuthStore.getState().user!.id, useWorkspaceStore.getState().currentId!, "s")
async function load(count: number, initial?: AssistantSnapshot) {
  useStreamStore.getState().setMessages("s", rows(count))
  const view = mount(initial)
  await waitFor(() => expect(status(`m${count - 1}`)).toBe("available"))
  vi.mocked(http.get).mockClear()
  return view
}
async function append(count: number) {
  await act(async () => useStreamStore.getState().setMessages("s", rows(count)))
}

describe("verified transcript intersection during a changed batch", () => {
  it("keeps 53 checked projections while one new ID waits for one real 54-ID read", async () => {
    await load(53)
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(54)
    expect(http.get).toHaveBeenCalledTimes(1)
    expect(ids(vi.mocked(http.get).mock.calls[0][0])).toEqual(rows(54).map((message) => message.id))
    for (const message of rows(53)) {
      expect(status(message.id)).toBe("available")
      expect(screen.getByTestId(message.id).textContent).toBe(`body:${message.id}`)
      expect(screen.getByTestId(message.id).getAttribute("data-checked-at")).toBe(at(1))
    }
    expect(status("m53")).toBe("pending")
    await act(async () => next.resolve({ messages: rows(54).map((message) => ({ ...message, source_checked_at: at(3) })) }))
    await waitFor(() => expect(status("m53")).toBe("available"))
    expect(screen.getByTestId("m0").getAttribute("data-checked-at")).toBe(at(3))
  })

  it.each(["failure", "cancel"])("invalidates the whole borrowed intersection on %s and cannot revive it with another key", async (outcome) => {
    const view = await load(2)
    const next = pending()
    let signal: AbortSignal | null | undefined
    vi.mocked(http.get).mockImplementationOnce((_, options) => { signal = options?.signal; return next.promise })
    await append(3)
    expect(status("m0")).toBe("available")
    if (outcome === "failure") await act(async () => next.reject(new Error("Source unavailable")))
    else await act(async () => { await view.client.cancelQueries({ queryKey: prefix() }); next.resolve({ messages: rows(3) }) })
    await waitFor(() => expect(status("m0")).not.toBe("available"))
    if (outcome === "cancel") expect(signal?.aborted).toBe(true)
    const retry = pending()
    vi.mocked(http.get).mockReturnValueOnce(retry.promise)
    await append(4)
    expect(status("m0")).toBe("pending")
    expect(status("m3")).toBe("pending")
    await act(async () => retry.resolve({ messages: rows(4) }))
    await waitFor(() => expect(status("m0")).toBe("available"))
    expect(http.get).toHaveBeenCalledTimes(2)
  })

  it("does not let an older cancelled refetch erase a newer successful batch", async () => {
    const view = await load(2)
    const old = pending(), next = pending(), after = pending()
    vi.mocked(http.get).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise).mockReturnValueOnce(after.promise)
    let fetching!: Promise<void>
    act(() => { fetching = view.client.refetchQueries({ queryKey: prefix(), type: "active" }) })
    await append(3) // Removing the old observer aborts its outstanding request.
    await act(async () => next.resolve({ messages: rows(3) }))
    await waitFor(() => expect(status("m2")).toBe("available"))
    await act(async () => { old.reject(new Error("Late old failure")); await fetching })
    await append(4)
    expect(status("m0")).toBe("available")
    expect(status("m2")).toBe("available")
    expect(status("m3")).toBe("pending")
    await act(async () => after.resolve({ messages: rows(4) }))
  })

  it("hides the intersection immediately on a source/reconnect barrier and rejects the late reply", async () => {
    const view = await load(2)
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(3)
    expect(status("m0")).toBe("available")
    act(() => requireFreshHistoryProof(view.client, { userId: "actor", workspaceId: "workspace" }, "s"))
    expect(status("m0")).toBe("pending")
    await act(async () => next.resolve({ messages: rows(3) }))
    await waitFor(() => expect(status("m0")).toBe("unavailable"))
    respond()
    await act(async () => view.client.refetchQueries({ queryKey: prefix(), type: "active" }))
    await waitFor(() => expect(status("m0")).toBe("available"))
  })

  it("cannot carry through a background/foreground transition or accept its old response", async () => {
    const view = await load(2)
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(3)
    const visibility = vi.spyOn(document, "visibilityState", "get")
    act(() => { visibility.mockReturnValue("hidden"); document.dispatchEvent(new Event("visibilitychange")) })
    expect(status("m0")).toBe("pending")
    act(() => { visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange")) })
    expect(status("m0")).toBe("pending")
    await act(async () => next.resolve({ messages: rows(3) }))
    await waitFor(() => expect(status("m0")).toBe("unavailable"))
    respond()
    await act(async () => view.client.refetchQueries({ queryKey: prefix(), type: "active" }))
    await waitFor(() => expect(status("m0")).toBe("available"))
  })

  it.each(["actor", "workspace", "session"])("does not borrow another %s scope or accept its late query", async (scope) => {
    const view = await load(2)
    const old = pending(), next = pending()
    vi.mocked(http.get).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    await append(3)
    expect(status("m0")).toBe("available")
    act(() => {
      if (scope === "actor") useAuthStore.setState({ user: { id: "other", username: "other", role: "user" } })
      if (scope === "workspace") useWorkspaceStore.setState({ currentId: "other" })
      if (scope === "session") { useStreamStore.getState().setMessages("other", rows(3, "other")); view.update(undefined, "other") }
    })
    expect(status("m0")).toBe("pending")
    await act(async () => old.resolve({ messages: rows(3) }))
    expect(status("m0")).toBe("pending")
    await act(async () => next.resolve({ messages: rows(3, scope === "session" ? "other" : "s") }))
    await waitFor(() => expect(status("m0")).toBe("available"))
    if (scope === "workspace") expect(vi.mocked(http.get).mock.calls[1][1]?.headers).toEqual({ "X-Workspace-Id": "other" })
  })

  it("keeps <=100-ID requests, prunes removed IDs and does not carry them when re-added", async () => {
    await load(101)
    const extra = pending()
    vi.mocked(http.get).mockReturnValueOnce(extra.promise)
    await append(102)
    expect(ids(vi.mocked(http.get).mock.calls[0][0])).toEqual(["m100", "m101"])
    expect(status("m0")).toBe("available")
    expect(status("m100")).toBe("available")
    expect(status("m101")).toBe("pending")
    await act(async () => extra.resolve({ messages: [row("m100"), row("m101")] }))
    await waitFor(() => expect(status("m101")).toBe("available"))
    respond()
    await act(async () => {
      useStreamStore.getState().clearMessages("s")
      useStreamStore.getState().setMessages("s", rows(102).slice(1))
    })
    await waitFor(() => expect(status("m101")).toBe("available"))
    expect(screen.queryByTestId("m0")).toBeNull()
    const readded = pending()
    vi.mocked(http.get).mockReturnValue(readded.promise)
    await append(102)
    expect(status("m0")).toBe("pending")
    expect(status("m1")).toBe("available")
    expect(vi.mocked(http.get).mock.calls.every(([url]) => ids(url).length <= 100)).toBe(true)
  })

  it("retains an original unavailable projection and never treats raw stream status as carry proof", async () => {
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...row("m0"), source_status: "unavailable", parts: [] }] })
    useStreamStore.getState().setMessages("s", rows(1))
    mount()
    await waitFor(() => expect(status("m0")).toBe("unavailable"))
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(2)
    expect(status("m0")).toBe("unavailable")
    expect(status("m1")).toBe("pending")
    expect(screen.getByTestId("m0").textContent).not.toContain("body:")
  })

  it("does not turn a click-time cache update into a carry grant", async () => {
    const view = await load(1)
    act(() => refreshTranscriptPages(view.client, prefix(), { messages: [{ ...row("m0"), source_checked_at: at(5) }] }))
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(2)
    expect(status("m0")).toBe("pending")
    expect(status("m1")).toBe("pending")
  })

  it("does not certify a mixed page when an older request retains a newer click-time projection", async () => {
    const view = await load(1)
    const old = pending()
    vi.mocked(http.get).mockReturnValueOnce(old.promise)
    let refreshing!: Promise<void>
    act(() => { refreshing = view.client.refetchQueries({ queryKey: prefix(), type: "active" }) })
    act(() => refreshTranscriptPages(view.client, prefix(), { messages: [{ ...row("m0"), source_checked_at: at(5) }] }))
    await act(async () => { old.resolve({ messages: [row("m0")] }); await refreshing })
    await waitFor(() => expect(screen.getByTestId("m0").getAttribute("data-checked-at")).toBe(at(5)))
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(2)
    expect(status("m0")).toBe("pending")
    expect(status("m1")).toBe("pending")
  })

  it.each(["available", "unavailable"] as const)("immediately drops a borrowed page changed to %s by copy while its replacement is pending", async (availability) => {
    const view = await load(2, state())
    const next = pending(), clicked = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise).mockReturnValueOnce(clicked.promise)
    await append(3)
    expect(status("m0")).toBe("available")
    fireEvent.click(screen.getByRole("button", { name: "Copy verified" }))
    await waitFor(() => expect(http.get).toHaveBeenCalledTimes(2))
    // Only the now-unobserved old key has data. No stream/snapshot rerender,
    // replacement response or event hint is used to reveal this revocation.
    await act(async () => clicked.resolve({ messages: [{ ...row("m0"), source_status: availability, source_checked_at: at(10),
      parts: availability === "available" ? row("m0").parts : [] }] }))
    expect(status("m0")).not.toBe("available")
    expect(screen.getByTestId("m0").textContent).not.toContain("body:")
    if (availability === "available") expect(copy).toHaveBeenCalledExactlyOnceWith("body:m0")
    else expect(copy).not.toHaveBeenCalled()
    await act(async () => next.resolve({ messages: rows(3) }))
    await waitFor(() => expect(status("m0")).toBe("unavailable"))
    vi.mocked(http.get).mockImplementation(async (url) => ({ messages: ids(url).map((id) => row(id, "s", at(21))) }))
    await act(async () => view.client.refetchQueries({ queryKey: prefix(), type: "active" }))
    await waitFor(() => expect(status("m0")).toBe("available"))
  })

  it("rejects an origin changed after render but before the borrowed map is committed", async () => {
    useStreamStore.getState().setMessages("s", rows(2))
    const view = mount(undefined, false, true)
    await waitFor(() => expect(status("m0")).toBe("available"))
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(3)
    expect(historyProofBarrier(view.client, { userId: "actor", workspaceId: "workspace" }, "s")).toBeGreaterThan(0)
    expect(status("m0")).not.toBe("available")
    await act(async () => next.resolve({ messages: rows(3) }))
    await waitFor(() => expect(status("m0")).toBe("unavailable"))
  })

  it("removes the old-origin subscription when the boundary unmounts", async () => {
    const view = await load(2)
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(3)
    expect(status("m0")).toBe("available")
    view.unmount()
    const before = historyProofBarrier(view.client, { userId: "actor", workspaceId: "workspace" }, "s")
    refreshTranscriptPages(view.client, prefix(), { messages: [{ ...row("m0"), source_status: "unavailable", parts: [], source_checked_at: at(10) }] })
    await act(async () => next.resolve({ messages: rows(3) }))
    expect(historyProofBarrier(view.client, { userId: "actor", workspaceId: "workspace" }, "s")).toBe(before)
  })

  it.each(["assistant.history.changed", "__connected"])("keeps the real %s event refresh authoritative over a changed batch", async (event) => {
    useStreamStore.getState().setMessages("s", rows(2))
    mount(undefined, true)
    await waitFor(() => expect(status("m0")).toBe("available"))
    await waitFor(() => expect(vi.mocked(http.get).mock.calls.some(([url]) => url.startsWith("/api/assistant/events"))).toBe(true))
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    await append(3)
    expect(status("m0")).toBe("available")
    act(() => listeners.get(event)?.forEach((listener) => listener({ sessionId: "s", generation: 1 })))
    expect(status("m0")).toBe("pending") // A real authority hint still hides old history; this is intentional.
    await act(async () => next.resolve({ messages: rows(3) }))
    await waitFor(() => expect(status("m0")).toBe("unavailable"))
    await waitFor(() => expect(status("m2")).toBe("available")) // The real coalesced event drain performs the next fresh read.
    expect(vi.mocked(http.get).mock.calls.filter(([url]) => url.startsWith("/api/assistant/messages")).length).toBe(3)
  })

  it("cannot revive a snapshot deny after omission or copy an older carried reply", async () => {
    const view = await load(2, state([{ message_id: "m0", sequence: 1, available: true }]))
    view.update(state([{ message_id: "m0", sequence: 1, available: false }], at(20)))
    view.update(state())
    const next = pending(), clicked = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise).mockReturnValueOnce(clicked.promise)
    await append(3)
    expect(status("m0")).toBe("unavailable")
    expect(status("m1")).toBe("available")
    fireEvent.click(screen.getByRole("button", { name: "Copy verified" }))
    await waitFor(() => expect(http.get).toHaveBeenCalledTimes(2))
    await act(async () => clicked.resolve({ messages: [row("m0")] }))
    expect(copy).not.toHaveBeenCalled()
    await act(async () => next.resolve({ messages: rows(3) }))
    await waitFor(() => expect(status("m2")).toBe("unavailable"))
    respond()
    await act(async () => view.client.refetchQueries({ queryKey: prefix(), type: "active" }))
    await waitFor(() => expect(status("m2")).toBe("available"))
    expect(status("m0")).toBe("unavailable")
    expect(mutate).not.toHaveBeenCalled()
  })

  it("does not read the new answer until its fresh body and signed display token are both visible", async () => {
    const first = { message_id: "m0", sequence: 1, available: true, display_token: "first-token" }
    const second = { message_id: "m1", sequence: 2, available: true, display_token: "second-token" }
    const view = await load(1, state([first]))
    act(() => observers.forEach((observer) => observer.show()))
    expect(mutate).toHaveBeenCalledExactlyOnceWith(first)
    const next = pending()
    vi.mocked(http.get).mockReturnValueOnce(next.promise)
    view.update(state([first, second]))
    await append(2)
    expect(status("m0")).toBe("available")
    expect(status("m1")).toBe("pending")
    act(() => observers.forEach((observer) => observer.show()))
    expect(mutate).toHaveBeenCalledTimes(1)
    await act(async () => next.resolve({ messages: rows(2) }))
    await waitFor(() => expect(status("m1")).toBe("available"))
    act(() => observers.forEach((observer) => observer.show()))
    expect(mutate).toHaveBeenCalledTimes(2)
    expect(mutate).toHaveBeenLastCalledWith(second)
  })
})

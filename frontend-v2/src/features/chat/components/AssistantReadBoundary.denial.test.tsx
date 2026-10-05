import { useContext, type ReactNode } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { assistantKeys, type AssistantSnapshot } from "../api/assistant"
import { AssistantReadContext } from "../hooks/assistant-read-context"
import { useVerifiedAssistantCopy } from "../hooks/useVerifiedAssistantCopy"
import { mergeTurns } from "../lib/turn-view"
import { rememberSnapshotDenials, sourceProjection } from "../lib/source-projection"
import { useStreamStore } from "../stores/stream"
import { AssistantReadBoundary } from "./AssistantReadBoundary"
import { AssistantTurn } from "./AssistantTurn"

const { copy, mutate } = vi.hoisted(() => ({ copy: vi.fn(), mutate: vi.fn() }))
vi.mock("@/shared/api/http", async (original) => ({ ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() } }))
vi.mock("../api/assistant", async (original) => ({ ...await original<typeof import("../api/assistant")>(), useAssistantReadCursor: () => ({ mutate }) }))
vi.mock("@/shared/hooks/useCopy", () => ({ useCopy: () => ({ copy, copied: false }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Read failed" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))
vi.mock("react-i18next", async (original) => ({ ...await original<typeof import("react-i18next")>(), useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("./Markdown", () => ({ default: ({ text }: { text: string }) => <p>{text}</p> }))
vi.mock("./meta/AssistantMeta", () => ({ AssistantMeta: () => null }))

const at = (micros: number) => `2026-10-06T01:00:00.${String(micros).padStart(6, "0")}+00:00`
const answer: MessageWithParts = {
  id: "answer", session_id: "s", role: "assistant", created_at: "", finish: "stop",
  source_status: "available", source_checked_at: at(1), error: { message: "SECRET_ERROR" },
  parts: [{ id: "text", type: "text", channel: "final", text: "SECRET_BODY" },
    { id: "thought", type: "reasoning", text: "SECRET_REASONING" },
    { id: "tool", type: "tool", tool: "read", status: "completed", input: { path: "SECRET_PATH" }, output: "SECRET_TOOL" }],
}
function snapshot(available?: boolean, checkedAt: string | null = at(2), sessionId = "s"): AssistantSnapshot {
  return { state: "ready", session: { id: sessionId, user_id: useAuthStore.getState().user?.id,
    workspace_id: useWorkspaceStore.getState().currentId },
    source_checked_at: checkedAt, last_seen_sequence: available === undefined ? 20 : 0,
    answers: available === undefined ? [] : [{ message_id: "answer", sequence: 10, available }],
  } as AssistantSnapshot
}
function Reply({ message }: { message: MessageWithParts }) {
  const [turn] = mergeTurns([message])
  if (turn.kind !== "assistant") throw new Error("Expected assistant turn")
  return <AssistantTurn messages={turn.messages} meta={turn.meta} streaming={false} sessionId={message.session_id} />
}
function CopyAction() {
  const { copyReply } = useVerifiedAssistantCopy("s", "answer", "SECRET_BODY")
  return <button onClick={() => { void copyReply() }}>Copy source</button>
}
function ProjectedState({ message }: { message: MessageWithParts }) {
  const context = useContext(AssistantReadContext)
  const projected = sourceProjection(message, context)
  return <output data-testid="projection">{projected.source_status}</output>
}
const clients: QueryClient[] = []
beforeEach(() => {
  vi.clearAllMocks()
  useAuthStore.setState({ user: { id: "actor", username: "actor", role: "user" } })
  useWorkspaceStore.setState({ currentId: "workspace" })
  useStreamStore.getState().clearMessages("s")
  useStreamStore.getState().clearMessages("other")
  useStreamStore.getState().setMessages("s", [answer])
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
})
afterEach(() => { cleanup(); for (const client of clients.splice(0)) client.clear(); vi.restoreAllMocks() })
function mount(initial = snapshot(true), message = answer, extra?: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  const tree = (state: AssistantSnapshot, current = message) => <QueryClientProvider client={client}>
    <AssistantReadBoundary snapshot={state}><Reply message={current} /><ProjectedState message={current} />{extra}</AssistantReadBoundary>
  </QueryClientProvider>
  const view = render(tree(initial))
  return { ...view, client, update: (state: AssistantSnapshot, current = message) => view.rerender(tree(state, current)) }
}
function refetch(client: QueryClient, sessionId = "s") {
  return client.refetchQueries({ queryKey: assistantKeys.transcripts(
    useAuthStore.getState().user!.id, useWorkspaceStore.getState().currentId!, sessionId), type: "active" })
}

describe("snapshot source denial survives an unread-only response", () => {
  it.each([10, 20])("keeps cached and in-flight transcript at %i hidden until a read starts after the completed deny", async (arrival) => {
    vi.mocked(http.get).mockResolvedValue({ messages: [answer] })
    const view = mount()
    await screen.findByText("SECRET_BODY")
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    let pending!: Promise<void>
    act(() => { pending = refetch(view.client) })
    view.update(snapshot(false, at(20))) // The snapshot began earlier, but its nested source read denied only at 20.
    expect(view.container.textContent).not.toContain("SECRET_")
    view.update(snapshot()) // Reading a newer answer removes this answer from the next snapshot.
    expect(view.container.textContent).not.toContain("SECRET_")
    await act(async () => { finish({ messages: [{ ...answer, source_checked_at: at(arrival) }] }); await pending })
    expect(view.container.textContent).not.toContain("SECRET_") // Before or equal to the completed deny is not later.
    const raw = { ...answer, source_checked_at: at(99) }
    view.update(snapshot(), raw)
    expect(view.container.textContent).not.toContain("SECRET_") // Raw message props cannot grant current authority.
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...answer, source_checked_at: at(21) }] })
    await act(() => refetch(view.client))
    await screen.findByText("SECRET_BODY")
    expect(screen.getByTestId("projection").textContent).toBe("available")
    // React Query reconciliation also retains the newer authorized projection over a late old response.
    await act(async () => view.client.setQueryData(assistantKeys.transcript("actor", "workspace", "s", ["answer"]), { messages: [answer] }))
    expect(view.client.getQueryData<{ messages: MessageWithParts[] }>(assistantKeys.transcript("actor", "workspace", "s", ["answer"]))?.messages[0].source_checked_at).toBe(at(21))
    expect(screen.getByText("SECRET_BODY")).toBeTruthy()
    expect(mutate).not.toHaveBeenCalled()
  })

  it("blocks a copy started before the denial even after the snapshot omits the answer", async () => {
    vi.mocked(http.get).mockResolvedValue({ messages: [answer] })
    const view = mount(snapshot(true), answer, <CopyAction />)
    await screen.findByText("SECRET_BODY")
    let finish!: (value: unknown) => void
    vi.mocked(http.get).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    fireEvent.click(screen.getByRole("button", { name: "Copy source" }))
    await waitFor(() => expect(vi.mocked(http.get).mock.calls.filter(([url]) => url.startsWith("/api/assistant/messages?")).length).toBe(2))
    view.update(snapshot(false))
    view.update(snapshot())
    await act(async () => finish({ messages: [answer] }))
    expect(copy).not.toHaveBeenCalled()
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...answer, source_checked_at: at(3) }] })
    fireEvent.click(screen.getByRole("button", { name: "Copy source" }))
    await waitFor(() => expect(copy).toHaveBeenCalledExactlyOnceWith("SECRET_BODY"))
  })

  it("retains the floor after a newer proof and ignores both older snapshot denies and raw-message claims", () => {
    const sourceDenials = new Map<string, string | null>()
    rememberSnapshotDenials(sourceDenials, snapshot(false, at(20)))
    const context = { snapshot: snapshot(), sourceDenials, transcript: new Map([["answer", { ...answer, source_checked_at: at(21) }]]) }
    expect(sourceProjection(answer, context).source_status).toBe("available")
    rememberSnapshotDenials(sourceDenials, snapshot(false, at(10)))
    const old = { ...context, transcript: new Map([["answer", answer]]) }
    expect(sourceProjection({ ...answer, source_checked_at: at(99) }, old).source_status).toBe("unavailable")
    expect(sourceDenials.get("answer")).toBe("2026-10-06T01:00:00.000020")
  })

  it.each([null, "not-a-clock"])("fails closed for a legacy false response without a comparable time (%s)", async (clock) => {
    vi.mocked(http.get).mockResolvedValue({ messages: [answer] })
    const view = mount(snapshot(false, clock))
    await waitFor(() => expect(http.get).toHaveBeenCalledTimes(1))
    view.update(snapshot())
    vi.mocked(http.get).mockResolvedValue({ messages: [{ ...answer, source_checked_at: at(50) }] })
    await act(() => refetch(view.client))
    expect(view.container.textContent).not.toContain("SECRET_")
  })

  it.each(["actor", "workspace", "session"])("does not carry a denial into another %s scope", async (scope) => {
    vi.mocked(http.get).mockResolvedValue({ messages: [answer] })
    const view = mount(snapshot(false))
    await waitFor(() => expect(http.get).toHaveBeenCalledTimes(1))
    const other = { ...answer, session_id: scope === "session" ? "other" : "s" }
    vi.mocked(http.get).mockResolvedValue({ messages: [other] })
    await act(async () => {
      if (scope === "actor") useAuthStore.setState({ user: { id: "other-actor", username: "other", role: "user" } })
      if (scope === "workspace") useWorkspaceStore.setState({ currentId: "other-workspace" })
      if (scope === "session") useStreamStore.getState().setMessages("other", [other])
      view.update(snapshot(undefined, at(3), other.session_id), other)
    })
    await screen.findByText("SECRET_BODY")
  })
})

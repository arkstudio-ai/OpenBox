// Real chat components, stream store, and send path. The test intercepts HTTP;
// no real account, provider call, sandbox, or billing mutation is involved.
import { Suspense, useEffect, useMemo, useRef, useState } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router"
import { ChatFlow, Composer, isBusyStatus, latestSuggestions, mergeTurns, useMessagesQuery, useSendChat, useStreamStore } from "../../src/features/chat"
import { useAuthStore } from "../../src/shared/api/auth-store"
import type { MessageWithParts, SessionStatus, SuggestionsPart } from "../../src/shared/types/api"
import i18n from "../../src/shared/i18n"
import "../../src/styles/index.css"

const sessionId = "suggestions-fixture"
const empty: MessageWithParts[] = []
const asyncPart: SuggestionsPart = { type: "suggestions", id: "async-part", items: [
  { label: "补充更多案例", prompt: "请为刚才的方案补充两个案例。", mode: "send" },
] }
useAuthStore.setState({ user: { id: "fixture-user", username: "fixture-user", role: "user" } as NonNullable<
  ReturnType<typeof useAuthStore.getState>["user"]
> })
const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

export function Fixture() {
  const query = useMessagesQuery(sessionId)
  useEffect(() => {
    if (query.data) useStreamStore.getState().setMessages(sessionId, query.data)
  }, [query.data])
  const messages = useStreamStore((s) => s.messages.get(sessionId) ?? empty)
  const status = useStreamStore((s) => s.status.get(sessionId)) ?? "idle"
  const turns = useMemo(() => mergeTurns(messages), [messages])
  const historyScrollRef = useRef<HTMLDivElement>(null)
  const [pending, setPending] = useState(false)
  const [readOnly, setReadOnly] = useState(false)
  const send = useSendChat(sessionId)
  const chips = latestSuggestions(turns, status, { readOnly, permissionCount: pending ? 1 : 0 })
  return (
    <main className="bg-bg text-ink flex h-dvh min-w-0 flex-col">
      <header className="border-hair flex flex-wrap gap-3 border-b p-3 text-xs">
        {(["idle", "busy", "waiting_input", "error"] as SessionStatus[]).map((value) => (
          <button key={value} onClick={() => useStreamStore.getState().setStatus(sessionId, value)}>{value}</button>
        ))}
        <button onClick={() => setPending((value) => !value)}>Toggle permission</button>
        <button onClick={() => setReadOnly((value) => !value)}>Toggle read only</button>
        <button onClick={() => useStreamStore.getState().addPart(sessionId, "a1", asyncPart)}>Late event</button>
        <button onClick={() => useStreamStore.getState().updatePart(sessionId, "a1", {
          ...asyncPart, id: "p1", status: "completed",
        })}>Complete suggestions</button>
        <button onClick={() => useStreamStore.getState().updatePart(sessionId, "a1", {
          ...asyncPart, id: "p1", status: "completed", items: [],
        })}>Empty suggestions</button>
        <button onClick={() => useStreamStore.getState().updatePart(sessionId, "a1", {
          ...asyncPart, id: "p1", status: "unavailable", items: [],
        })}>Failed suggestions</button>
        <button onClick={() => { document.documentElement.dataset.mode = "dark" }}>Dark</button>
        <button onClick={() => { document.documentElement.dataset.mode = "light" }}>Light</button>
        <button onClick={() => void i18n.changeLanguage("en-US")}>English</button>
        <button onClick={() => void i18n.changeLanguage("zh-CN")}>中文</button>
      </header>
      <ChatFlow turns={turns} sessionId={sessionId} busy={isBusyStatus(status)} historyScrollRef={historyScrollRef} />
      <Composer busy={isBusyStatus(status)} suggestions={chips} historyScrollRef={historyScrollRef} sessionKey={sessionId} sessionModel="openai/chat-picked"
        agents={[{ name: "build" }, { name: "plan" }]} onSubmit={send}
        onStop={() => useStreamStore.getState().setStatus(sessionId, "idle")} />
    </main>
  )
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <MemoryRouter><Suspense fallback={null}><Fixture /></Suspense></MemoryRouter>
  </QueryClientProvider>,
)

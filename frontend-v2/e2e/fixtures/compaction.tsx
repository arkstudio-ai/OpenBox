import { useState } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { AssistantTurn } from "../../src/features/chat/components/AssistantTurn"
import { ContextRing } from "../../src/features/chat/components/composer/ContextRing"
import { mergeTurns } from "../../src/features/chat/lib/turn-view"
import { mergeSnapshotMessages } from "../../src/features/chat/stores/stream"
import type { MessageWithParts } from "../../src/shared/types/api"
import "../../src/styles/index.css"
import "../../src/shared/i18n"

const base = { session_id: "fixture-session", created_at: "2026-09-18T00:00:00Z" }
const working: MessageWithParts = {
  ...base, id: "m1", role: "assistant", finish: "tool_calls",
  parts: [
    { id: "start", type: "step-start", step: 1 },
    { id: "think", type: "reasoning", text: "先检查结果，再继续下一步任务。" },
    { id: "tool", type: "tool", tool: "bash", status: "completed", input: { command: "echo ready" }, output: "ready" },
  ],
}
const request: MessageWithParts = { ...base, id: "m2", role: "user", agent: "compaction", parts: [{ id: "marker", type: "compaction", auto: true }] }
const summary: MessageWithParts = { ...base, id: "m3", role: "assistant", agent: "compaction", parent_id: "m2", summary: false, parts: [{ id: "text", type: "text", text: "## Goal\n内部摘要：验收口令青竹-0917。" }] }
const reply: MessageWithParts = { ...base, id: "m4", role: "assistant", finish: "stop", parts: [{ id: "answer", type: "text", text: "已继续执行下一步" }] }

function Fixture() {
  const [state, setState] = useState(new URLSearchParams(location.search).get("state") ?? "running")
  const finish = state === "failed" ? "error" : state === "interrupted" ? "aborted" : state === "running" ? null : "stop"
  let messages: MessageWithParts[] = [working, request, { ...summary, finish, summary: finish === "stop" }]
  if (state === "stale") messages = mergeSnapshotMessages(messages, [working, request, summary])
  if (["completed", "stale", "repeat"].includes(state)) messages.push(reply)
  if (state === "repeat") messages.push(
    { ...request, id: "m5" },
    { ...summary, id: "m6", parent_id: "m5", summary: true, finish: "stop" },
  )
  const [turn] = mergeTurns(messages)
  if (turn.kind !== "assistant") throw new Error("Expected a single process turn")
  return <main className="bg-bg text-ink min-h-dvh p-4 sm:p-8">
    <header className="mb-8 flex flex-wrap gap-4">
      {[["completed", "继续执行"], ["repeat", "再次压缩"], ["failed", "压缩失败"], ["interrupted", "压缩中断"], ["stale", "迟到历史"]].map(([value, label]) =>
        <button key={value} onClick={() => setState(value)}>{label}</button>)}
      <ContextRing used={60000} limit={100000} compactionThreshold={60000} />
    </header>
    <div className="mx-auto max-w-3xl">
      <AssistantTurn messages={turn.messages} meta={turn.meta} sessionId="fixture-session" streaming={state === "running" || state === "stale"} />
    </div>
  </main>
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <BrowserRouter><Fixture /></BrowserRouter>
  </QueryClientProvider>,
)

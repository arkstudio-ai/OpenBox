// Real client components; all HTTP is intercepted by the companion spec.
// No real account, model, sandbox, billing or backend mutation is used.
import { Suspense, useEffect, useState } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { Composer } from "../../src/features/chat/components/Composer"
import { QuestionDock } from "../../src/features/chat/components/QuestionDock"
import { useQuestionsQuery } from "../../src/features/chat/api/question"
import { usePendingStore } from "../../src/features/chat/stores/pending"
import { useAuthStore } from "../../src/shared/api/auth-store"
import type { QuestionRequest } from "../../src/shared/types/api"
import "../../src/styles/index.css"
import "../../src/shared/i18n"

const question: QuestionRequest = {
  id: "fixture-ask",
  user_id: "fixture-user",
  session_id: "fixture-session",
  status: "pending",
  questions: [
    { question: "请选择时长", options: [{ label: "30秒" }, { label: "60秒" }] },
    { question: "请选择字幕", options: [{ label: "保留" }, { label: "不保留" }] },
    { question: "请选择语气", options: [{ label: "轻松" }, { label: "正式" }] },
  ],
  draft_revision: 0,
}
useAuthStore.setState({
  user: { id: "fixture-user", username: "fixture-user", role: "user" } as NonNullable<
    ReturnType<typeof useAuthStore.getState>["user"]
  >,
})
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

export function Fixture() {
  const query = useQuestionsQuery()
  const questions = usePendingStore((s) => s.questions)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (query.data) usePendingStore.getState().setQuestions(query.data.items, query.data.read)
  }, [query.data])
  return (
    <main className="bg-bg text-ink flex h-dvh min-w-0 flex-col">
      <header className="flex flex-wrap gap-2 p-2">
        <button onClick={() => usePendingStore.getState().addQuestion(question)}>模拟新 ask</button>
        <button onClick={() => setBusy(true)}>模拟运行</button>
        <span role="status">{query.isFetching ? "读取中" : "读取完成"}</span>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {(questions.get("fixture-session") ?? []).map((q) => (
          <QuestionDock key={q.id} request={q} />
        ))}
      </div>
      <Composer
        busy={busy}
        onStop={() => setBusy(false)}
        onSubmit={async () => {
          setBusy(true)
        }}
        agents={[{ name: "build" }, { name: "plan" }]}
        sessionKey="fixture-session"
      />
    </main>
  )
}
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <Suspense fallback={null}>
      <Fixture />
    </Suspense>
  </QueryClientProvider>,
)

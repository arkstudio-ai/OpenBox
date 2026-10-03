import { useCallback, useEffect, useRef } from "react"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { Spinner } from "@/shared/ui/Spinner"
import type { QuestionRequest } from "@/shared/types/api"
import { useResourceMention } from "@/features/resources"
import { AssistantReadBoundary, AssistantRequests, AssistantTaskList, sendAssistantTurn, useAssistantEvents,
  useAssistantSnapshot, useEnsureAssistant, QuestionDock, type SendRequest } from "@/features/chat"
import { ChatSessionView } from "./ChatRoute"

export default function AssistantRoute() {
  const userId = useAuthStore((state) => state.user?.id)
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return <AssistantEntry key={`${userId}:${workspaceId}`} workspaceId={workspaceId} />
}

function AssistantEntry({ workspaceId }: { workspaceId: string | null }) {
  const { t } = useTranslation("chat")
  const snapshot = useAssistantSnapshot()
  const ensure = useEnsureAssistant()
  const attempted = useRef(false)
  const errorMessage = useApiErrorMessage()
  useAssistantEvents(snapshot.data?.session?.id)
  const create = ensure.mutate
  useEffect(() => {
    if (snapshot.data?.state === "not_created" && !attempted.current) {
      attempted.current = true
      create()
    }
  }, [snapshot.data?.state, create])
  const mainId = snapshot.data?.session?.id
  const sendRequest = useCallback<SendRequest>((vars) => {
    if (!mainId) return Promise.reject(new Error("Assistant entry is unavailable"))
    return sendAssistantTurn(mainId, workspaceId, vars)
  }, [mainId, workspaceId])
  const error = snapshot.error ?? ensure.error
  if (error) return <div className="m-auto max-w-lg space-y-3 p-6 text-sm" role="alert">
    <p>{errorMessage(error)}</p><button type="button" className="underline" disabled={ensure.isPending || snapshot.isFetching}
      onClick={() => ensure.error ? create() : void snapshot.refetch()}>{t("assistant.reload")}</button>
  </div>
  if (!mainId || !snapshot.data) return <div className="flex flex-1 items-center justify-center"><Spinner /></div>
  return <AssistantReadBoundary snapshot={snapshot.data}>
    <AssistantTaskList />
    <AssistantRequests renderQuestion={renderQuestion} />
    <div className="min-h-0 flex-1"><ChatSessionView key={mainId} sessionId={mainId} assistant sendRequest={sendRequest} /></div>
  </AssistantReadBoundary>
}

function ResourceQuestion({ request }: { request: QuestionRequest }) {
  const scope = useResourceMention(request.session_id, request.assistant?.project_id)
  return <QuestionDock request={request} resourceScope={scope} />
}

function renderQuestion(request: QuestionRequest) {
  return request.questions.some((item) => item.allow_attachments)
    ? <ResourceQuestion request={request} /> : <QuestionDock request={request} />
}

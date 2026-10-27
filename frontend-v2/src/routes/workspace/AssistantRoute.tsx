import { useCallback, useEffect, useRef } from "react"
import { useTranslation } from "react-i18next"
import { useSearchParams } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { Spinner } from "@/shared/ui/Spinner"
import type { QuestionRequest } from "@/shared/types/api"
import { useResourceMention } from "@/features/resources"
import { AssistantIntroEntry, AssistantReadBoundary, AssistantRequests, AssistantWelcome, sendAssistantTurn,
  useAssistantEvents, useAssistantSnapshot, useEnsureAssistant, QuestionDock, AssistantNotificationTarget,
  type SendRequest } from "@/features/chat"
import { introStartsByItself } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { ChatSessionView } from "./ChatRoute"
import { CompanionWelcome } from "@/features/chat"

interface AssistantRouteProps { compact?: boolean; realtimeSurface?: "assistant" | "workspace" }
export default function AssistantRoute({ compact = false, realtimeSurface }: AssistantRouteProps) {
  const userId = useAuthStore((state) => state.user?.id)
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return <AssistantEntry key={`${userId}:${workspaceId}`} workspaceId={workspaceId} compact={compact} realtimeSurface={realtimeSurface} />
}

/** The personal assistant: one long conversation with a secretary. Tasks live
 *  in "我的任务"; what a delegated task waits on the user for is a card at the
 *  end of the conversation, right above the composer, answered here as in the
 *  task. Questions in the user's other conversations stay a dismissible hint. */
function AssistantEntry({ workspaceId, compact, realtimeSurface }: { workspaceId: string | null } & AssistantRouteProps) {
  const [params] = useSearchParams()
  const taskId = params.get("task")
  const resultId = params.get("result")
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
  if (error) return <div className="m-auto max-w-lg space-y-3 p-6 text-center text-sm" role="alert">
    <p>{errorMessage(error)}</p><button type="button" className="border-hair hover:bg-hairsoft rounded-full border px-4 py-1.5"
      disabled={ensure.isPending || snapshot.isFetching}
      onClick={() => ensure.error ? create() : void snapshot.refetch()}>{t("assistant.reload")}</button>
  </div>
  if (!mainId || !snapshot.data) return <div className="flex flex-1 items-center justify-center"><Spinner /></div>
  return <AssistantReadBoundary snapshot={snapshot.data}>
    {!compact && taskId && resultId && <AssistantNotificationTarget key={`${taskId}:${resultId}`} taskId={taskId} resultId={resultId} />}
    <div className="min-h-0 flex-1">
      <ChatSessionView key={mainId} sessionId={mainId} assistant sendRequest={sendRequest} realtimeSurface={realtimeSurface} compact={compact}
        welcome={(fill) => compact ? <CompanionWelcome onPick={fill} /> : <AssistantWelcome onPick={fill} />}
        extraFooter={<AssistantRequests conversation renderQuestion={renderQuestion} />}
        aside={compact ? undefined : ({ fill, quiet }) => <AssistantIntroEntry quiet={quiet} onPick={fill} />}
        onSend={wentStraightToWork} />
    </div>
  </AssistantReadBoundary>
}

/** The first message sent while the first meeting is still open: the person went straight to work.
 *  The meeting steps aside (once they have an answer, a one-line reminder offers it again). */
function wentStraightToWork({ empty }: { empty: boolean }) {
  const { assistantMeta, recordIntro } = useAppearanceStore.getState()
  if (empty && assistantMeta && introStartsByItself(assistantMeta))
    void recordIntro({ event: "bypass" }).catch(() => undefined)
}

function ResourceQuestion({ request }: { request: QuestionRequest }) {
  const scope = useResourceMention(request.session_id, request.assistant?.project_id)
  return <QuestionDock request={request} resourceScope={scope} />
}

function renderQuestion(request: QuestionRequest) {
  return request.questions.some((item) => item.allow_attachments)
    ? <ResourceQuestion request={request} /> : <QuestionDock request={request} />
}

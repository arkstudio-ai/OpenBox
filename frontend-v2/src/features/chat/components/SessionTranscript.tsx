// Read-only view of one conversation, for showing what a scheduled run did
// inside its task page rather than as a chat of its own. Same history and
// socket bridge as the chat route, so a run still going streams in; no
// composer, no pending cards.
import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import type { MessageWithParts } from "@/shared/types/api"
import { useSessionQuery } from "../api/message-actions"
import { useChatEvents } from "../hooks/useChatEvents"
import { useChatHistory } from "../hooks/useChatHistory"
import { mergeTurns } from "../lib/turn-view"
import { isBusyStatus, useStreamStore } from "../stores/stream"
import { ChatFlow } from "./ChatFlow"

const EMPTY_MESSAGES: MessageWithParts[] = []

export function SessionTranscript({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation("chat")
  useChatEvents(sessionId)
  const session = useSessionQuery(sessionId, { poll: true })
  const liveStatus = useStreamStore((s) => s.status.get(sessionId))
  const busy = isBusyStatus(liveStatus ?? session.data?.status)
  const history = useChatHistory(sessionId, busy)
  const messages = useStreamStore((s) => s.messages.get(sessionId) ?? EMPTY_MESSAGES)
  const turns = useMemo(() => mergeTurns(messages), [messages])

  if (messages.length === 0) {
    if (history.messagesQ.isLoading) {
      return (
        <div className="flex flex-1 items-center justify-center py-10">
          <Spinner className="size-5" />
        </div>
      )
    }
    if (history.messagesQ.isError) {
      return (
        <p className="text-n600 flex flex-1 items-center justify-center p-6 text-sm">
          {t("transcript.loadFailed")}
        </p>
      )
    }
    if (!busy) {
      return (
        <p className="text-n600 flex flex-1 items-center justify-center p-6 text-sm">
          {t("transcript.empty")}
        </p>
      )
    }
  }

  return (
    <ChatFlow
      key={sessionId}
      turns={turns}
      sessionId={sessionId}
      busy={busy}
      hasMore={history.hasMore}
      loadingOlder={history.loadingOlder}
      onLoadOlder={history.loadOlder}
    />
  )
}

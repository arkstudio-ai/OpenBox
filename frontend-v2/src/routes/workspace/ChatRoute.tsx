import { useEffect } from "react"
import { Link, useParams, useSearchParams } from "react-router"
import { useTranslation } from "react-i18next"
import { ChatSurface } from "@/features/chat"
import { useSessionQuery } from "@/features/chat/api/message-actions"
import { useConfigQuery } from "@/features/chat/api/config"
import { useResourceMention } from "@/features/resources"
import { usePanelStore } from "@/features/workbench"
import { CONTROL_PARAM, PANEL_PARAM, paths, readPanelRequest } from "@/shared/router/paths"
import { TeamComposer } from "./TeamComposer"
import { TeamTurnTools } from "./TeamTurnTools"

export { ComposerAccess } from "@/features/chat/components/ChatSurface"

export default function ChatRoute() {
  const { sessionId = "" } = useParams()
  const { t } = useTranslation("teams")
  const [searchParams, setSearchParams] = useSearchParams()
  const session = useSessionQuery(sessionId)
  const config = useConfigQuery()
  const resourceScope = useResourceMention(sessionId)
  useEffect(() => {
    const request = readPanelRequest(searchParams)
    if (!request) return
    usePanelStore.getState().openKind(request.kind, { desktopControl: request.control })
    const next = new URLSearchParams(searchParams)
    next.delete(PANEL_PARAM)
    next.delete(CONTROL_PARAM)
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])
  const member = session.data?.kind === "team_member"
  return (
    <ChatSurface
      sessionId={sessionId}
      resourceScope={resourceScope}
      ComposerComponent={TeamComposer}
      headerSlot={
        member && session.data?.parent_id ? (
          <Link
            to={paths.teamChat(session.data.parent_id)}
            className="border-hair text-a700 border-b px-5 py-2 text-xs"
          >
            {t("backToTeam")}
          </Link>
        ) : undefined
      }
      renderTurnTools={
        !member && config.data?.team_ui_enabled
          ? (parts) => <TeamTurnTools parts={parts} sessionId={sessionId} />
          : undefined
      }
    />
  )
}

import { useCallback, useEffect, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { useSearchParams } from "react-router"
import { ApiError } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { useAccessScope, useSessionHeader } from "../api/queries"
import { targetKey } from "../api/registry"
import { useTrajectoryAccess } from "../stores/access"
import { useTrajectoryView } from "../stores/view"
import { parseDetailParams, serializeDetailParams, type DetailParams } from "../utils/params"
import { AccessNotice } from "./AccessNotice"
import { RecordedSession } from "./session/RecordedSession"
import { SessionHeaderBar } from "./session/SessionHeaderBar"

interface TrajectorySessionPageProps {
  sessionId: string
}

/**
 * One user's session: identity header, then the recorded trajectory for
 * monitoring and replay. The route keys this page by session, and the view
 * store is bound to (viewer, session) before anything reads it, so one
 * target's selection or playhead never applies to another.
 */
export function TrajectorySessionPage({ sessionId }: TrajectorySessionPageProps) {
  const { t } = useTranslation("admin-trajectories")
  const errorMessage = useApiErrorMessage()
  const [search, setSearch] = useSearchParams()
  const detail = useMemo(() => parseDetailParams(search), [search])
  const { viewerId } = useAccessScope()
  const denied = useTrajectoryAccess((s) => s.denied)
  const key = targetKey(viewerId, sessionId)
  const bound = useTrajectoryView((s) => s.targetKey === key)
  const bindTarget = useTrajectoryView((s) => s.bindTarget)
  const header = useSessionHeader(sessionId)

  useEffect(() => {
    // A no-op once bound: later URL changes are written by the page, not read back.
    bindTarget(key, { playhead: detail.at, record: detail.record })
  }, [bindTarget, detail.at, detail.record, key])

  const updateDetail = useCallback(
    (patch: Partial<DetailParams>) =>
      setSearch((current) => serializeDetailParams({ ...parseDetailParams(current), ...patch }), {
        replace: true,
      }),
    [setSearch],
  )
  const backHref = paths.adminTrajectories(detail.back || undefined)

  if (denied) return <AccessNotice denial={denied} />
  // A terminal header error is shown even unbound: deletion forgets the target
  // (view reset, caches dropped) and it is never rebound. Header data still
  // renders only once the view is bound to this target.
  if (!header.error && (header.isLoading || !bound)) {
    return (
      <div className="flex items-center justify-center gap-2 py-16" data-testid="trajectory-session-loading">
        <Spinner className="size-5" />
        <span className="text-n600 text-sm">{t("session.loading")}</span>
      </div>
    )
  }
  if (header.error || !header.data) {
    const status = header.error instanceof ApiError ? header.error.status : null
    const message =
      status === 410 ? t("session.gone") : status === 404 ? t("session.notFound") : errorMessage(header.error)
    return (
      <p
        role="alert"
        className="border-hair bg-card text-dangerink rounded-xl border p-5 text-sm"
        data-testid="trajectory-session-error"
      >
        {message}
      </p>
    )
  }
  const data = header.data
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="trajectory-session">
      {data.trajectory_id ? (
        <RecordedSession
          sessionId={sessionId}
          header={data}
          detail={detail}
          onDetail={updateDetail}
          backHref={backHref}
        />
      ) : (
        <>
          <SessionHeaderBar header={data} backHref={backHref} seq={null} live coverageStart={null} />
          <p
            className="border-hair bg-card text-n700 rounded-xl border p-5 text-sm"
            data-testid="trajectory-not-recorded"
          >
            {t("session.notRecorded")}
          </p>
        </>
      )}
    </div>
  )
}

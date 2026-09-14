import { useEffect } from "react"
import { useTranslation } from "react-i18next"
import { usePlaybackTimer, usePosition } from "../../hooks/usePlayback"
import { useTrajectorySocket } from "../../hooks/useTrajectorySocket"
import { useTrajectorySync } from "../../hooks/useTrajectorySync"
import { useTrajectoryView } from "../../stores/view"
import type { SessionHeader } from "../../types/protocol"
import type { DetailParams } from "../../utils/params"
import { ExportControl } from "./ExportControl"
import { PendingPosition } from "./PendingPosition"
import { SessionHeaderBar } from "./SessionHeaderBar"
import { SessionWorkspace } from "./SessionWorkspace"
import { SyncNotice } from "./SyncNotice"
import { useDetailUrlSync } from "./useDetailUrlSync"

interface RecordedSessionProps {
  sessionId: string
  header: SessionHeader
  detail: DetailParams
  onDetail: (patch: Partial<DetailParams>) => void
  backHref: string
}

/**
 * A session with a recording: the ordered event stream, the replay position
 * built from it, the watermark socket, and the workspace at that position.
 * A pinned position (playhead, or `at` in the URL before it is applied) never
 * falls back to anything read at the head while it loads.
 */
export function RecordedSession({ sessionId, header, detail, onDetail, backHref }: RecordedSessionProps) {
  const { t } = useTranslation("admin-trajectories")
  const { sync, snapshot } = useTrajectorySync(sessionId, true)
  const position = usePosition(sync, snapshot)
  usePlaybackTimer(snapshot, position)
  useTrajectorySocket(sessionId, header.owner.user_id, sync)
  useDetailUrlSync(detail, onDetail)
  const playhead = useTrajectoryView((s) => s.playhead)
  const returnToLive = useTrajectoryView((s) => s.returnToLive)
  const phase = snapshot?.phase
  // A pinned position is already "loading" before the engine opens, when a
  // request for older history is ignored; ask again once the engine is live.
  // The engine de-duplicates a load already in flight.
  useEffect(() => {
    if (sync && phase === "live" && position?.status === "loading") sync.ensurePosition(position.seq)
  }, [phase, position, sync])
  const ready = position?.status === "ready" ? position : null
  const wiped = snapshot?.phase === "gone" || snapshot?.phase === "denied"
  const seq = ready?.seq ?? null
  return (
    <>
      <SessionHeaderBar
        header={header}
        backHref={backHref}
        seq={seq}
        live={playhead === null}
        coverageStart={ready?.state.coverage_start ?? header.coverage_start}
      >
        <ExportControl
          sessionId={sessionId}
          throughSeq={seq}
          enabled={header.capabilities.export && !wiped}
        />
      </SessionHeaderBar>
      {snapshot && <SyncNotice snapshot={snapshot} position={position} onReturnToLive={returnToLive} />}
      {wiped && (
        <p
          className="border-hair bg-card text-n700 rounded-xl border p-5 text-sm"
          data-testid="trajectory-session-wiped"
        >
          {t(snapshot?.phase === "gone" ? "session.gone" : "session.accessEnded")}
        </p>
      )}
      {!wiped && snapshot && ready && (
        <SessionWorkspace
          sessionId={sessionId}
          snapshot={snapshot}
          position={ready}
          detail={detail}
          onDetail={onDetail}
        />
      )}
      {!wiped && !ready && position?.status !== "error" && (
        <PendingPosition
          sessionId={sessionId}
          headSeq={header.committed_seq}
          pinnedSeq={playhead ?? detail.at}
        />
      )}
    </>
  )
}

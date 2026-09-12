import { Navigate, useParams } from "react-router"
import { TrajectorySessionPage, useTrajectoryAccessWatcher } from "@/features/admin-trajectories"
import { paths } from "@/shared/router/paths"

/**
 * 轨迹监控 › 会话详情. Keyed by the target so switching sessions remounts the
 * page: no selection, playhead or in-flight read of one target can survive
 * into another.
 */
export default function AdminTrajectorySessionRoute() {
  useTrajectoryAccessWatcher()
  const { sessionId } = useParams()
  if (!sessionId) return <Navigate to={paths.adminTrajectories()} replace />
  return <TrajectorySessionPage key={sessionId} sessionId={sessionId} />
}

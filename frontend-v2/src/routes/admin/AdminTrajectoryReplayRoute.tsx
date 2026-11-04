import { useParams } from "react-router"
import { TrajectoryReplayPage, useTrajectoryAccessWatcher } from "@/features/admin-trajectories"

/** 轨迹监控 › 简明模式 › 会话回放 — one session as the person saw it. */
export default function AdminTrajectoryReplayRoute() {
  useTrajectoryAccessWatcher()
  const { userId = "", sessionId = "" } = useParams()
  return <TrajectoryReplayPage userId={userId} sessionId={sessionId} />
}

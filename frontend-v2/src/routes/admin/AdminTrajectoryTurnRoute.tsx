import { useParams } from "react-router"
import { TrajectoryTurnPage, useTrajectoryAccessWatcher } from "@/features/admin-trajectories"

/** 轨迹监控 › 简明模式 › 轮详情 — one turn as a step flow. */
export default function AdminTrajectoryTurnRoute() {
  useTrajectoryAccessWatcher()
  const { userId = "", sessionId = "", turnId = "" } = useParams()
  return <TrajectoryTurnPage userId={userId} sessionId={sessionId} turnId={turnId} />
}

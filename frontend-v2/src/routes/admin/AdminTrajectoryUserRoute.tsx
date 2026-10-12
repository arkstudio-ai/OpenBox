import { useParams } from "react-router"
import { TrajectoryUserTurnsPage, useTrajectoryAccessWatcher } from "@/features/admin-trajectories"

/** 轨迹监控 › 简明模式 › 某用户的对话轮 — one user's turns across sessions. */
export default function AdminTrajectoryUserRoute() {
  useTrajectoryAccessWatcher()
  const { userId = "" } = useParams()
  return <TrajectoryUserTurnsPage userId={userId} />
}

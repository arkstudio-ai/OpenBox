import { TrajectoryListPage, useTrajectoryAccessWatcher } from "@/features/admin-trajectories"

/** 轨迹监控 › 会话列表 — cross-user sessions; filters and paging live in the URL. */
export default function AdminTrajectoriesRoute() {
  useTrajectoryAccessWatcher()
  return <TrajectoryListPage />
}

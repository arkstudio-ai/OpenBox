import { Navigate, useLocation } from "react-router"
import { TrajectoryUsersPage, useTrajectoryAccessWatcher } from "@/features/admin-trajectories"
import { isLegacyListSearch } from "@/features/admin-trajectories/lite"
import { paths } from "@/shared/router/paths"

/**
 * 轨迹监控 › 简明模式 › 用户列表 — the column's landing page. A bookmark of the
 * professional session list from before the lite view (recognised by its own
 * filter keys) is sent on to that list with its query intact.
 */
export default function AdminTrajectoriesRoute() {
  useTrajectoryAccessWatcher()
  const { search } = useLocation()
  const query = search.startsWith("?") ? search.slice(1) : search
  if (isLegacyListSearch(new URLSearchParams(query))) {
    return <Navigate to={paths.adminTrajectorySessions(query)} replace />
  }
  return <TrajectoryUsersPage />
}

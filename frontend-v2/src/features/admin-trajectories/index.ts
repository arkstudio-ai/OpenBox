// Public surface of the admin trajectory viewer: the pages the admin console
// routes mount (professional list and session detail; lite users, turns and
// turn detail), and the access binding those routes install.
export { TrajectoryListPage } from "./components/TrajectoryListPage"
export { TrajectorySessionPage } from "./components/TrajectorySessionPage"
export { TrajectoryUsersPage, TrajectoryUserTurnsPage, TrajectoryTurnPage } from "./lite"
export { useTrajectoryAccessWatcher } from "./hooks/useTrajectoryAccessWatcher"

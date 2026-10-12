// Which of the two views the operator used last. The URL decides what is
// showing; this only informs navigation that lands on the column root.
export type TrajectoryMode = "lite" | "pro"

export const MODE_STORAGE_KEY = "admin-trajectories.mode"

export function rememberMode(mode: TrajectoryMode): void {
  try {
    window.localStorage.setItem(MODE_STORAGE_KEY, mode)
  } catch {
    // Storage may be blocked; the toggle still navigates.
  }
}

export function rememberedMode(): TrajectoryMode | null {
  try {
    const value = window.localStorage.getItem(MODE_STORAGE_KEY)
    return value === "lite" || value === "pro" ? value : null
  } catch {
    return null
  }
}

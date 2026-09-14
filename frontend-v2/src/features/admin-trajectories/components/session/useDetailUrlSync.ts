import { useEffect } from "react"
import { useTrajectoryView } from "../../stores/view"
import type { DetailParams } from "../../utils/params"

/**
 * Mirrors the replay position and selection into the URL (replace, not push)
 * so a refresh or a shared link reopens the same view. While playing the URL
 * is left alone; it catches up when playback stops.
 */
export function useDetailUrlSync(
  detail: DetailParams,
  onDetail: (patch: Partial<DetailParams>) => void,
): void {
  const playhead = useTrajectoryView((s) => s.playhead)
  const selected = useTrajectoryView((s) => s.selectedRecordId)
  const playing = useTrajectoryView((s) => s.playing)

  useEffect(() => {
    if (playing) return
    if (detail.at === playhead && detail.record === selected) return
    onDetail({ at: playhead, record: selected })
  }, [detail.at, detail.record, onDetail, playhead, playing, selected])
}

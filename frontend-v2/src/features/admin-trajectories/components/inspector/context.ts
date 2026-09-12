import { createContext, useContext } from "react"
import type { Seq, TrajectoryEvent, TrajectoryStatistics } from "../../types/protocol"
import type { RecordTree, ViewRecord } from "../../utils/view"
import type { Ordinals } from "../session/ordinals"

/**
 * What every inspector panel reads besides its record: the one watermark all
 * content is fetched at, the recorded clock for open intervals, and navigation
 * to related records. Kept in context so panels stay under the prop budget.
 * Everything here describes the shown position — never the live head.
 */
export interface InspectorEnv {
  sessionId: string
  /** The position shown. Payloads, details and totals are all read at this seq. */
  throughSeq: Seq
  /** Recorded time at the position (replay) or now (live), for open intervals. */
  clock: string | null
  live: boolean
  /** Records projected at `throughSeq`, with their captured data. */
  records: Readonly<Record<string, ViewRecord>>
  tree: RecordTree
  /** Every locally known event at or before the position. */
  events: readonly TrajectoryEvent[]
  /** Session totals at the position, computed from the same projection. */
  statistics: TrajectoryStatistics
  ordinals: Ordinals
  select: (recordId: string) => void
}

export const InspectorContext = createContext<InspectorEnv | null>(null)

export function useInspector(): InspectorEnv {
  const env = useContext(InspectorContext)
  if (!env) throw new Error("Inspector panels must render inside InspectorContext")
  return env
}

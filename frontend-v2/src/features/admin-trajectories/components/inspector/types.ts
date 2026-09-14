import type { TrajectoryEvent, TrajectoryRecord } from "../../types/protocol"

/** A record as the detail endpoint returns it at the inspector's watermark. */
export type InspectedRecord = TrajectoryRecord & { events?: TrajectoryEvent[] }

export interface PanelProps {
  record: InspectedRecord
}

export const NS = "admin-trajectories"

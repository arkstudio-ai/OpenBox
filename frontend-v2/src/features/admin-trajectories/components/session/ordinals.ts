// Readable Turn / Run / Step / Request numbers for rows and breadcrumbs. The
// persisted ids stay the identity; a number is only a label, assigned in order
// of first recorded appearance, so it never depends on what is filtered.
import type { RecordNode, ViewRecord } from "../../utils/view"

export type OrdinalScope = "turn" | "run" | "step" | "request"

export type Ordinals = Readonly<Record<OrdinalScope, ReadonlyMap<string, number>>>

export const ORDINAL_SCOPES: readonly OrdinalScope[] = ["turn", "run", "step", "request"]

const ID_OF: Readonly<Record<OrdinalScope, (record: ViewRecord) => string | null>> = {
  turn: (record) => record.turn_id,
  run: (record) => record.run_id,
  step: (record) => record.step_id,
  request: (record) => record.request_id,
}

/** Records — or tree nodes, which RecordTree keeps in seq order — must arrive in seq order. */
export function buildOrdinals(items: Iterable<ViewRecord | RecordNode>): Ordinals {
  const maps: Record<OrdinalScope, Map<string, number>> = {
    turn: new Map(),
    run: new Map(),
    step: new Map(),
    request: new Map(),
  }
  for (const item of items) {
    const record = "record" in item ? item.record : item
    for (const scope of ORDINAL_SCOPES) {
      const id = ID_OF[scope](record)
      if (id && !maps[scope].has(id)) maps[scope].set(id, maps[scope].size + 1)
    }
  }
  return maps
}

export interface OrdinalPart {
  scope: OrdinalScope
  value: number
}

/** The numbered position of a record, outermost first; unknown ids are skipped. */
export function positionOf(record: ViewRecord, ordinals: Ordinals): OrdinalPart[] {
  const parts: OrdinalPart[] = []
  for (const scope of ORDINAL_SCOPES) {
    const id = ID_OF[scope](record)
    const value = id ? ordinals[scope].get(id) : undefined
    if (value !== undefined) parts.push({ scope, value })
  }
  return parts
}

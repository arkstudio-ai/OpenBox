// Links from one record to the records it names. Every link is built from an
// id the record itself carries and only offered when that record exists at the
// current position — a link never reaches into the future.
import type { ViewRecord } from "./view"

export type RelationKind =
  | "request"
  | "assistant"
  | "tool"
  | "parentTool"
  | "agent"
  | "parentAgent"
  | "turn"
  | "run"
  | "step"
  | "nextAttempt"
  | "resumedRun"
  | "sourceRequest"
  | "system"

export interface Relation {
  kind: RelationKind
  recordId: string
}

const CALL_KINDS = new Set(["permission", "question", "job", "artifact"])

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null
}

export function relationsFor(record: ViewRecord, records: Readonly<Record<string, ViewRecord>>): Relation[] {
  const links: Relation[] = []
  const add = (kind: RelationKind, id: string | null, prefix: string) => {
    if (!id) return
    const recordId = `${prefix}:${id}`
    if (
      recordId !== record.record_id &&
      records[recordId] &&
      !links.some((link) => link.recordId === recordId)
    ) {
      links.push({ kind, recordId })
    }
  }
  if (record.kind === "request") {
    add("assistant", record.request_id, "assistant")
    add("system", record.request_id, "system")
  } else {
    add("request", record.request_id, "request")
  }
  if (record.kind === "tool") add("parentTool", record.parent_call_id, "tool")
  if (CALL_KINDS.has(record.kind)) add("tool", record.call_id, "tool")
  if (record.kind === "agent") add("parentAgent", record.parent_agent_id, "agent")
  else add("agent", record.agent_id, "agent")
  if (record.kind !== "turn") add("turn", record.turn_id, "turn")
  if (record.kind !== "run") add("run", record.run_id, "run")
  if (record.kind !== "step") add("step", record.step_id, "step")
  const data = record.data ?? {}
  add("nextAttempt", str(data.next_request_id), "request")
  add("resumedRun", str(data.resume_of_run_id), "run")
  add("sourceRequest", str(data.source_request_id), "request")
  return links
}

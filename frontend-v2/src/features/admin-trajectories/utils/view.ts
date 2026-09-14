// Browser-side view of a projection: the hierarchy, visible rows and Agent
// tree. Parents come only from persisted relation ids (parent_call_id,
// call_id, request_id, agent_id, step_id, run_id, turn_id) — never from "the
// row above", which would misfile concurrent work. Nothing here changes record
// values.
import type { RecordSummary, TrajectoryRecord } from "../types/protocol"
import { cmpSeq } from "./seq"

export type ViewRecord = RecordSummary & Partial<Pick<TrajectoryRecord, "data" | "blocks">>

export interface RecordNode {
  record: ViewRecord
  parentId: string | null
  children: string[]
}

export interface RecordTree {
  nodes: ReadonlyMap<string, RecordNode>
  roots: readonly string[]
}

const REQUEST_CHILD_KINDS = new Set(["assistant", "tool", "retry", "system", "late_result"])
const CALL_CHILD_KINDS = new Set(["permission", "question", "job", "artifact"])
/** Facts of the task itself rather than of one execution: they sit directly under their Turn. */
const TURN_LEVEL_KINDS = new Set(["user", "baseline", "settings", "history"])
const LIFECYCLE_KINDS = new Set(["turn", "run", "step"])

export function compareRecords(a: ViewRecord, b: ViewRecord): number {
  return (
    cmpSeq(a.start_seq, b.start_seq) || (a.record_id < b.record_id ? -1 : a.record_id > b.record_id ? 1 : 0)
  )
}

/**
 * Most specific first: Sub-tool › Tool › Request › (child) Agent › Step › Run › Turn.
 * The first candidate that exists at this position and does not form a cycle wins.
 */
function candidateParents(record: ViewRecord): string[] {
  const { kind } = record
  const ids: string[] = []
  if (kind === "tool" && record.parent_call_id) ids.push(`tool:${record.parent_call_id}`)
  if (CALL_CHILD_KINDS.has(kind) && record.call_id) ids.push(`tool:${record.call_id}`)
  if (REQUEST_CHILD_KINDS.has(kind) && record.request_id) ids.push(`request:${record.request_id}`)
  if (!LIFECYCLE_KINDS.has(kind) && !TURN_LEVEL_KINDS.has(kind)) {
    if (kind === "agent" && record.parent_agent_id) ids.push(`agent:${record.parent_agent_id}`)
    if (kind !== "agent" && record.agent_id) ids.push(`agent:${record.agent_id}`)
    if (record.step_id) ids.push(`step:${record.step_id}`)
  }
  if (!TURN_LEVEL_KINDS.has(kind) && kind !== "run" && kind !== "turn" && record.run_id)
    ids.push(`run:${record.run_id}`)
  if (kind !== "turn" && record.turn_id) ids.push(`turn:${record.turn_id}`)
  return ids
}

function createsCycle(nodes: Map<string, RecordNode>, childId: string, parentId: string): boolean {
  let cursor: string | null = parentId
  const seen = new Set<string>()
  while (cursor) {
    if (cursor === childId || seen.has(cursor)) return true
    seen.add(cursor)
    cursor = nodes.get(cursor)?.parentId ?? null
  }
  return false
}

export function buildTree(records: Iterable<ViewRecord>): RecordTree {
  const sorted = [...records].sort(compareRecords)
  const nodes = new Map<string, RecordNode>()
  for (const record of sorted) nodes.set(record.record_id, { record, parentId: null, children: [] })
  const roots: string[] = []
  for (const record of sorted) {
    const node = nodes.get(record.record_id)!
    const parentId = candidateParents(record).find(
      (id) => id !== record.record_id && nodes.has(id) && !createsCycle(nodes, record.record_id, id),
    )
    if (parentId) {
      node.parentId = parentId
      nodes.get(parentId)!.children.push(record.record_id)
    } else {
      roots.push(record.record_id)
    }
  }
  return { nodes, roots }
}

export interface RecordFilters {
  kinds: readonly string[]
  statuses: readonly string[]
  /** Restrict to this agent and its descendants. */
  agentId: string | null
  text: string
}

export const NO_FILTERS: RecordFilters = { kinds: [], statuses: [], agentId: null, text: "" }

export function hasFilters(filters: RecordFilters): boolean {
  return (
    filters.kinds.length > 0 ||
    filters.statuses.length > 0 ||
    !!filters.agentId ||
    filters.text.trim().length > 0
  )
}

/** The agent plus every agent spawned below it, following persisted parent ids. */
export function agentScope(records: Iterable<ViewRecord>, agentId: string): Set<string> {
  const parents = new Map<string, string>()
  for (const record of records) {
    if (record.kind === "agent" && record.agent_id && record.parent_agent_id)
      parents.set(record.agent_id, record.parent_agent_id)
  }
  const scope = new Set([agentId])
  let grew = true
  while (grew) {
    grew = false
    for (const [child, parent] of parents) {
      if (scope.has(parent) && !scope.has(child)) {
        scope.add(child)
        grew = true
      }
    }
  }
  return scope
}

function matches(record: ViewRecord, filters: RecordFilters, scope: Set<string> | null): boolean {
  if (filters.kinds.length && !filters.kinds.includes(record.kind)) return false
  if (filters.statuses.length && !filters.statuses.includes(record.status ?? "")) return false
  if (scope && !(record.agent_id && scope.has(record.agent_id))) return false
  const needle = filters.text.trim().toLocaleLowerCase()
  if (!needle) return true
  return [record.title, record.preview, record.result_preview, record.record_id, record.status_reason].some(
    (value) => typeof value === "string" && value.toLocaleLowerCase().includes(needle),
  )
}

export interface RecordRow {
  record: ViewRecord
  depth: number
  childCount: number
  collapsed: boolean
  /** Shown only because a descendant matches the filters. */
  context: boolean
}

/**
 * Depth-first rows for the virtualised list. With filters, ancestors of a
 * match stay visible (marked `context`) so a hit is never shown detached
 * from its request, tool or agent.
 */
export function flattenTree(
  tree: RecordTree,
  collapsed: Readonly<Record<string, true>>,
  filters: RecordFilters = NO_FILTERS,
): RecordRow[] {
  const filtering = hasFilters(filters)
  const scope = filters.agentId
    ? agentScope(
        [...tree.nodes.values()].map((node) => node.record),
        filters.agentId,
      )
    : null
  const visible = new Map<string, boolean>()
  const include = (id: string): boolean => {
    const cached = visible.get(id)
    if (cached !== undefined) return cached
    const node = tree.nodes.get(id)!
    const self = !filtering || matches(node.record, filters, scope)
    const childHit = node.children.map(include).some(Boolean)
    const result = self || childHit
    visible.set(id, result)
    return result
  }
  const rows: RecordRow[] = []
  const walk = (id: string, depth: number) => {
    if (!include(id)) return
    const node = tree.nodes.get(id)!
    const isCollapsed = !!collapsed[id] && !filtering
    rows.push({
      record: node.record,
      depth,
      childCount: node.children.length,
      collapsed: isCollapsed,
      context: filtering && !matches(node.record, filters, scope),
    })
    if (!isCollapsed) for (const child of node.children) walk(child, depth + 1)
  }
  for (const root of tree.roots) walk(root, 0)
  return rows
}

/** Ancestor chain, root first — the inspector breadcrumb. */
export function ancestry(tree: RecordTree, recordId: string): ViewRecord[] {
  const chain: ViewRecord[] = []
  let cursor = tree.nodes.get(recordId)?.parentId ?? null
  const seen = new Set<string>()
  while (cursor && !seen.has(cursor)) {
    seen.add(cursor)
    const node = tree.nodes.get(cursor)
    if (!node) break
    chain.unshift(node.record)
    cursor = node.parentId
  }
  return chain
}

export interface AgentTreeNode {
  agentId: string
  /** The agent record, when one was projected; the main agent usually has none. */
  recordId: string | null
  name: string | null
  status: string | null
  children: AgentTreeNode[]
}

/** Agents as recorded, plus referenced agents without a record (e.g. the main agent). */
export function agentTree(records: Iterable<ViewRecord>): AgentTreeNode[] {
  const nodes = new Map<string, AgentTreeNode>()
  const parents = new Map<string, string | null>()
  const ensure = (agentId: string) => {
    let node = nodes.get(agentId)
    if (!node) {
      node = { agentId, recordId: null, name: null, status: null, children: [] }
      nodes.set(agentId, node)
    }
    return node
  }
  for (const record of [...records].sort(compareRecords)) {
    if (record.kind === "agent" && record.agent_id) {
      const node = ensure(record.agent_id)
      node.recordId = record.record_id
      node.name = record.title
      node.status = record.status
      if (record.parent_agent_id) {
        ensure(record.parent_agent_id)
        parents.set(record.agent_id, record.parent_agent_id)
      }
    } else if (record.agent_id) {
      ensure(record.agent_id)
    }
  }
  const roots: AgentTreeNode[] = []
  for (const [agentId, node] of nodes) {
    const parent = parents.get(agentId)
    if (parent && nodes.has(parent) && parent !== agentId) nodes.get(parent)!.children.push(node)
    else roots.push(node)
  }
  return roots
}

// The step flow of a turn (lite plan §4.3), built from the server's classified
// record summaries. Pure: tested without rendering.
import { resolveTool } from "./labels"
import type { LiteLabels, LiteStep } from "./types"

export type FlowItem =
  | { kind: "step"; step: LiteStep }
  /** Consecutive low-value tools of one group, folded into one card. */
  | { kind: "group"; group: string; label: string; steps: LiteStep[]; failed: number }
  /** A sub-agent with the steps it ran. */
  | { kind: "agent"; step: LiteStep; children: FlowItem[] }
  | { kind: "gap"; step: LiteStep }

export interface Flow {
  items: FlowItem[]
  /** Retries are an annotation on the turn, not cards. */
  retries: number
  /** Steps the flow left out (`hidden` kinds and control records). */
  hidden: number
}

const SHOWN = new Set([
  "user_input",
  "assistant",
  "tool",
  "question",
  "permission",
  "takeover",
  "agent",
  "job",
  "artifact",
  "gap",
])

function seqOf(step: LiteStep): number {
  const value = Number(step.start_seq)
  return Number.isFinite(value) ? value : 0
}

function fold(steps: LiteStep[], labels: LiteLabels): FlowItem[] {
  const items: FlowItem[] = []
  for (const step of steps) {
    if (step.step_type === "gap") {
      items.push({ kind: "gap", step })
      continue
    }
    if (step.step_type === "tool" && step.collapsible && step.group) {
      const last = items[items.length - 1]
      if (last && last.kind === "group" && last.group === step.group) {
        last.steps.push(step)
        continue
      }
      items.push({
        kind: "group",
        group: step.group,
        label: labels.groups[step.group]?.label || step.label || resolveTool(toolId(step), labels).label,
        steps: [step],
        failed: 0,
      })
      continue
    }
    items.push({ kind: "step", step })
  }
  // A group of one is just that step; the fold only pays off from two.
  return items.map((item) =>
    item.kind === "group" && item.steps.length === 1 ? { kind: "step", step: item.steps[0]! } : item,
  )
}

/**
 * Order the steps, hand every sub-agent its own steps (folded under the agent
 * card) and fold runs of collapsible tools. Steps of an agent the turn never
 * recorded a spawn for stay inline, so nothing recorded disappears.
 */
export function buildFlow(steps: readonly LiteStep[], mainAgentId: string | null, labels: LiteLabels): Flow {
  const ordered = [...steps].sort((a, b) => seqOf(a) - seqOf(b) || a.record_id.localeCompare(b.record_id))
  const retries = ordered.filter((step) => step.step_type === "retry").length
  const shown = ordered.filter((step) => SHOWN.has(step.step_type) && !isNoise(step, mainAgentId))
  const agents = new Map(
    shown.filter((step) => step.step_type === "agent" && step.agent_id).map((step) => [step.agent_id!, step]),
  )
  const main = new Set<string | null>([mainAgentId, null])
  const topLevel: LiteStep[] = []
  const byAgent = new Map<string, LiteStep[]>()
  for (const step of shown) {
    const owner = step.agent_id ?? null
    if (step.step_type !== "agent" && !main.has(owner) && owner && agents.has(owner)) {
      byAgent.set(owner, [...(byAgent.get(owner) ?? []), step])
    } else {
      topLevel.push(step)
    }
  }
  const items = fold(topLevel, labels).map((item): FlowItem => {
    if (item.kind === "step" && item.step.step_type === "agent" && item.step.agent_id) {
      return { kind: "agent", step: item.step, children: fold(byAgent.get(item.step.agent_id) ?? [], labels) }
    }
    return item
  })
  for (const item of items)
    if (item.kind === "group") item.failed = item.steps.filter((step) => isFailed(step)).length
  return { items, retries, hidden: ordered.length - shown.length }
}

/** A permission the policy answered by itself, or the main agent's own record: never a card. */
function isNoise(step: LiteStep, mainAgentId: string | null): boolean {
  if (step.step_type === "permission" && step.status === "completed" && (step.duration_ms ?? 0) < 1000)
    return true
  // A model output that only called tools has nothing to read: the tool cards already tell the story.
  if (step.step_type === "assistant" && !isFailed(step) && (!step.preview || step.preview === "tool calls"))
    return true
  return step.step_type === "agent" && !!mainAgentId && step.agent_id === mainAgentId
}

export function toolId(step: LiteStep): string {
  return step.tool_id || step.title
}

export function isFailed(step: LiteStep): boolean {
  return step.status === "failed" || step.status === "denied" || step.status === "timed_out"
}

/** Whether the step is still open (the card shows a live marker). */
export function isOpen(step: LiteStep): boolean {
  return (
    step.status === "running" ||
    step.status === "streaming" ||
    step.status === "waiting" ||
    step.status === "pending"
  )
}

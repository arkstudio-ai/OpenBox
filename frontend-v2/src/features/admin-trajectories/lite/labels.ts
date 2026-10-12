// Words for the lite view. Tool names come from the backend dictionary
// (GET /lite/labels, backend/trajectory/lite_labels.json) so operations can
// rename an action without a frontend release; statuses and the one-line
// conclusion are translated here. Resolution mirrors trajectory/lite.py:
// exact id, then the longest matching prefix, then the id itself.
import type { TFunction } from "i18next"
import type { StatusTone } from "@/shared/ui/StatusPill"
import { EMPTY_LABELS, isTurnStatus, type LiteLabels, type TurnStatus, type TurnSummary } from "./types"

export interface ResolvedTool {
  label: string
  group: string | null
  /** Consecutive steps of this group fold into one card unless one of them failed. */
  collapse: boolean
  /** Known to the dictionary (exact or prefix), or shown by its raw id. */
  known: boolean
}

export function resolveTool(toolId: string, labels: LiteLabels = EMPTY_LABELS): ResolvedTool {
  const exact = labels.tools[toolId]
  if (exact) {
    const group = exact.group ?? null
    return {
      label: exact.label || toolId,
      group,
      collapse: !!(group && labels.groups[group]?.collapse),
      known: true,
    }
  }
  const prefix = Object.keys(labels.prefixes)
    .filter((candidate) => toolId.startsWith(candidate))
    .sort((a, b) => b.length - a.length)[0]
  if (prefix !== undefined) {
    const entry = labels.prefixes[prefix]!
    const group = entry.group ?? null
    return {
      label: entry.label || toolId,
      group,
      collapse: !!(group && labels.groups[group]?.collapse),
      known: true,
    }
  }
  return { label: toolId, group: null, collapse: false, known: false }
}

export function groupLabel(group: string, labels: LiteLabels): string {
  return labels.groups[group]?.label || group
}

export const TURN_STATUS_TONES: Record<TurnStatus, StatusTone> = {
  completed: "muted",
  completed_with_errors: "warn",
  failed: "danger",
  waiting: "accent",
  running: "ok",
  cancelled: "muted",
  unknown: "warn",
}

export function turnStatusTone(status: string): StatusTone {
  return isTurnStatus(status) ? TURN_STATUS_TONES[status] : "muted"
}

/** Translated status; an unknown value is shown as itself rather than mislabelled. */
export function turnStatusLabel(status: string, t: TFunction): string {
  return isTurnStatus(status) ? t(`lite.turnStatus.${status}`) : status
}

export function waitingKindLabel(kind: string | null, t: TFunction): string {
  if (!kind) return t("lite.waiting.generic")
  return t(`lite.waiting.${kind}`, { defaultValue: kind })
}

/** Actions an operator cares about seeing named in the conclusion of a successful turn. */
const KEY_ACTIONS = [
  "desktop_publish",
  "douyin_publish",
  "video_generate",
  "video_compose",
  "image_gen",
  "share_file",
  "desktop_login",
  "autopilot_run",
]

function clip(text: string | null | undefined, max: number): string | null {
  if (!text) return null
  const trimmed = text.trim()
  if (!trimmed) return null
  return trimmed.length > max ? `${trimmed.slice(0, max)}…` : trimmed
}

function firstFailedTool(summary: TurnSummary, labels: LiteLabels): string | null {
  const failed = Object.entries(summary.tool_counts).find(([, count]) => count.failed > 0)
  return failed ? resolveTool(failed[0], labels).label : null
}

/**
 * The one-line verdict of a turn (lite plan §5.3): rules, not a model.
 * `elapsed` is how long a waiting turn has waited, already formatted.
 */
export function conclusion(
  summary: TurnSummary,
  labels: LiteLabels,
  t: TFunction,
  elapsed?: string | null,
): string {
  const reason = clip(summary.status_reason, 60)
  const failedTool = firstFailedTool(summary, labels)
  switch (summary.status) {
    case "failed":
      if (failedTool)
        return reason
          ? t("lite.verdict.failedStep", { action: failedTool, reason })
          : t("lite.verdict.failedStepNoReason", { action: failedTool })
      return reason ? t("lite.verdict.failedReply", { reason }) : t("lite.verdict.failedReplyNoReason")
    case "waiting":
      return elapsed
        ? t("lite.verdict.waiting", { kind: waitingKindLabel(summary.waiting_kind, t), elapsed })
        : t("lite.verdict.waitingNoElapsed", { kind: waitingKindLabel(summary.waiting_kind, t) })
    case "completed_with_errors":
      return t("lite.verdict.completedWithErrors", {
        action: failedTool ?? t("lite.verdict.someStep"),
        count: summary.tool_failures,
      })
    case "completed": {
      const key = Object.entries(summary.tool_counts)
        .filter(([id, count]) => KEY_ACTIONS.includes(id) && count.total > count.failed)
        .sort((a, b) => KEY_ACTIONS.indexOf(a[0]) - KEY_ACTIONS.indexOf(b[0]))[0]
      if (key)
        return t("lite.verdict.completedAction", {
          action: resolveTool(key[0], labels).label,
          count: key[1].total - key[1].failed,
        })
      return clip(summary.assistant_preview, 60) ?? t("lite.verdict.completed")
    }
    case "running": {
      return t("lite.verdict.running")
    }
    case "cancelled":
      return t("lite.verdict.cancelled")
    case "unknown":
      return t("lite.verdict.unknown")
    default:
      return summary.status
  }
}

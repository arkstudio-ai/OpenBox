import type { ToolPart } from "@/shared/types/api"

/** What one of the assistant's memory write tools did, as shown under its answer. */
export type MemoryReceipt =
  | { kind: "remembered"; memoryId: string; summary: string; revision?: number }
  | { kind: "already_remembered" }
  | { kind: "refused" }
  | { kind: "paused" }
  | { kind: "updated"; memoryId: string; summary: string }
  | { kind: "forgotten" }

const TOOLS: ReadonlySet<string> = new Set(["memory.remember", "memory.update", "memory.forget"])

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function output(text?: string): Record<string, unknown> {
  if (!text) return {}
  try { return record(JSON.parse(text)) } catch { return {} }
}

/** Only a completed memory tool's structured result counts; prose never does.
 *  The live frame carries the JSON output; a reloaded transcript keeps the
 *  persisted `assistant_memory` metadata, which wins where both are present. */
export function memoryReceipt(part: ToolPart): MemoryReceipt | null {
  if (!TOOLS.has(part.tool) || part.status !== "completed") return null
  const value = { ...output(part.output), ...record(part.metadata?.assistant_memory) }
  const memoryId = typeof value.memory_id === "string" && value.memory_id ? value.memory_id : null
  const summary = typeof value.summary === "string" && value.summary.trim() ? value.summary : null
  switch (`${part.tool}:${String(value.state)}`) {
    case "memory.remember:remembered":
      return memoryId && summary
        ? { kind: "remembered", memoryId, summary, revision: typeof value.revision === "number" ? value.revision : undefined }
        : null
    case "memory.remember:already_remembered": return { kind: "already_remembered" }
    case "memory.remember:refused":
    case "memory.update:refused": return { kind: "refused" }
    case "memory.remember:paused": return { kind: "paused" }
    case "memory.update:updated": return memoryId && summary ? { kind: "updated", memoryId, summary } : null
    case "memory.forget:forgotten": return { kind: "forgotten" }
    default: return null
  }
}

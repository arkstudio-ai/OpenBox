// Inspector tabs by record kind (SESSION_TRAJECTORY_UI_SPEC §4). Tabs depend
// only on the kind — and, for system snapshots, on whether the record is an
// update, which never changes for a given record — so a tool keeps the same
// five tabs from "arguments streaming" to "finished". Missing content shows an
// availability state inside the tab instead of hiding the tab.
import type { ViewRecord } from "./view"

export const TAB_IDS = [
  "summary",
  "preview",
  "raw",
  "source",
  "input",
  "options",
  "usage",
  "timing",
  "arguments",
  "result",
  "schema",
  "diff",
  "systemPrompt",
  "tools",
  "task",
  "tree",
  "request",
  "decision",
  "questions",
  "answer",
  "impact",
  "related",
  "attempts",
  "error",
  "output",
  "progress",
  "content",
  "versions",
  "events",
] as const
export type TabId = (typeof TAB_IDS)[number]

const CONTENT: readonly TabId[] = ["summary", "preview", "raw", "source"]
const STATE_CHANGE: readonly TabId[] = ["summary", "content", "source"]

const BY_KIND: Readonly<Record<string, readonly TabId[]>> = {
  user: CONTENT,
  context: CONTENT,
  assistant: CONTENT,
  request: ["summary", "input", "options", "usage", "timing"],
  tool: ["summary", "arguments", "result", "schema", "timing"],
  agent: ["summary", "input", "tree", "result", "usage", "timing"],
  permission: ["summary", "request", "decision", "timing"],
  question: ["summary", "questions", "answer", "source", "timing"],
  interrupt: ["summary", "impact", "related", "timing"],
  resume: ["summary", "input", "related", "timing"],
  retry: ["summary", "attempts", "error", "timing"],
  compaction: ["summary", "input", "output", "diff", "usage", "timing"],
  job: ["summary", "input", "progress", "result", "timing"],
  artifact: ["summary", "preview", "versions", "source"],
  takeover: ["summary", "content", "source", "timing"],
  turn: ["summary", "related", "timing"],
  run: ["summary", "related", "timing"],
  step: ["summary", "related", "timing"],
}

function isSystemUpdate(record: ViewRecord): boolean {
  const before = record.data?.before
  return before !== undefined && before !== null
}

/** Stable tab list for a record. The raw event sequence is always last. */
export function tabsFor(record: ViewRecord): TabId[] {
  // `artifact_type` arrives with the first artifact event, so the tab set stays stable.
  if (record.kind === "artifact" && record.data?.artifact_type === "file_diff") {
    return ["summary", "preview", "diff", "versions", "source", "events"]
  }
  if (record.kind === "system" || record.kind === "tool_catalog") {
    const system: TabId[] = ["systemPrompt", "tools", "source"]
    return [...(isSystemUpdate(record) ? (["diff"] as TabId[]) : []), ...system, "events"]
  }
  return [...(BY_KIND[record.kind] ?? STATE_CHANGE), "events"]
}

export function defaultTab(record: ViewRecord): TabId {
  return tabsFor(record)[0]
}

import { describe, expect, it } from "vitest"
import type { ProjectionState, TrajectoryEvent } from "../types/protocol"
import { classifyValue, fieldState } from "./availability"
import { replay } from "./projector"
import { tabsFor } from "./tabs"
import { elapsedUntil, formatDuration, formatInstant } from "./time"
import { agentTree, ancestry, buildTree, flattenTree, NO_FILTERS, type ViewRecord } from "./view"

interface GoldenFixture {
  events: TrajectoryEvent[]
  expected_state: ProjectionState
}

const fixtureFiles = import.meta.glob<GoldenFixture>("../../../../../backend/trajectory/fixtures/*.json", {
  eager: true,
  import: "default",
})
const golden = Object.entries(fixtureFiles).find(([path]) => path.endsWith("/session_v1.json"))![1]
const records = Object.values(replay(golden.events).records)

function record(overrides: Partial<ViewRecord>): ViewRecord {
  return {
    ...records.find((row) => row.record_id === "tool:call_a")!,
    ...overrides,
  }
}

describe("record hierarchy", () => {
  const tree = buildTree(records)

  it("files children under persisted relations, not the previous row", () => {
    expect(tree.nodes.get("assistant:req_a")?.parentId).toBe("request:req_a")
    expect(tree.nodes.get("tool:call_a")?.parentId).toBe("request:req_a")
    expect(tree.nodes.get("permission:perm_a")?.parentId).toBe("tool:call_a")
    expect(tree.nodes.get("request:req_retry")?.parentId).toBe("agent:agent_child")
    expect(tree.nodes.get("retry:evt_fixture_28")?.parentId).toBe("request:req_child")
    expect(tree.roots).toEqual(["turn:turn_a"])
  })

  it("keeps the Turn › Run › Step › Request lifecycle ancestry", () => {
    expect(tree.nodes.get("run:run_a")?.parentId).toBe("turn:turn_a")
    expect(tree.nodes.get("run:run_b")?.parentId).toBe("turn:turn_a")
    expect(tree.nodes.get("step:step_a")?.parentId).toBe("run:run_a")
    // This fixture's request events carry run_id but no step_id.
    expect(tree.nodes.get("request:req_a")?.parentId).toBe("run:run_a")
    expect(tree.nodes.get("question:q_a")?.parentId).toBe("run:run_a")
    expect(tree.nodes.get("agent:agent_child")?.parentId).toBe("run:run_b")
    expect(tree.nodes.get("resume:run_b")?.parentId).toBe("run:run_b")
    expect(tree.nodes.get("user:msg_user")?.parentId).toBe("turn:turn_a")
    const stepped = record({
      record_id: "request:stepped",
      kind: "request",
      request_id: "stepped",
      call_id: null,
      step_id: "step_a",
      start_seq: "35",
    })
    const withStep = buildTree([...records, stepped])
    expect(withStep.nodes.get("request:stepped")?.parentId).toBe("step:step_a")
    expect(ancestry(withStep, "request:stepped").map((row) => row.record_id)).toEqual([
      "turn:turn_a",
      "run:run_a",
      "step:step_a",
    ])
  })

  it("keeps a record without a known parent at the top instead of guessing", () => {
    const orphan = record({
      record_id: "tool:late",
      call_id: "late",
      request_id: "req_missing",
      turn_id: null,
      run_id: null,
      step_id: null,
      agent_id: null,
      start_seq: "40",
    })
    const withOrphan = buildTree([...records, orphan])
    expect(withOrphan.nodes.get("tool:late")?.parentId).toBeNull()
  })

  it("nests a sub-tool under its parent call even when rows interleave", () => {
    const child = record({ record_id: "tool:sub", call_id: "sub", parent_call_id: "call_a", start_seq: "14" })
    const sibling = record({ record_id: "tool:other", call_id: "other", start_seq: "13" })
    const nested = buildTree([...records, sibling, child])
    expect(nested.nodes.get("tool:sub")?.parentId).toBe("tool:call_a")
    expect(ancestry(nested, "tool:sub").map((row) => row.record_id)).toEqual([
      "turn:turn_a",
      "run:run_a",
      "request:req_a",
      "tool:call_a",
    ])
  })

  it("collapses a group and reveals filter matches with their ancestors", () => {
    const collapsed = flattenTree(tree, { "request:req_a": true })
    expect(collapsed.some((row) => row.record.record_id === "tool:call_a")).toBe(false)
    const filtered = flattenTree(tree, { "request:req_a": true }, { ...NO_FILTERS, kinds: ["permission"] })
    const ids = filtered.map((row) => row.record.record_id)
    expect(ids).toEqual(["turn:turn_a", "run:run_a", "request:req_a", "tool:call_a", "permission:perm_a"])
    expect(filtered.find((row) => row.record.record_id === "request:req_a")?.context).toBe(true)
  })

  it("scopes to an agent and its descendants", () => {
    const rows = flattenTree(tree, {}, { ...NO_FILTERS, agentId: "agent_child" })
    const matched = rows.filter((row) => !row.context).map((row) => row.record.record_id)
    expect(matched).toContain("request:req_retry")
    expect(matched).not.toContain("request:req_a")
  })

  it("builds the agent tree from persisted parent ids, including the unrecorded main agent", () => {
    const agents = agentTree(records)
    expect(agents).toHaveLength(1)
    expect(agents[0]).toMatchObject({ agentId: "agent_a", recordId: null })
    expect(agents[0].children[0]).toMatchObject({
      agentId: "agent_child",
      recordId: "agent:agent_child",
      name: "子 Agent",
    })
  })
})

describe("inspector tabs", () => {
  it("gives a tool the same five tabs while pending and when finished", () => {
    const pending = record({ status: "pending", end_seq: null })
    const done = record({ status: "completed", end_seq: "19" })
    expect(tabsFor(pending)).toEqual(["summary", "arguments", "result", "schema", "timing", "events"])
    expect(tabsFor(done)).toEqual(tabsFor(pending))
  })

  it("distinguishes request, assistant and user inputs", () => {
    const byId = (id: string) => records.find((row) => row.record_id === id)!
    expect(tabsFor(byId("request:req_a"))).toEqual([
      "summary",
      "input",
      "options",
      "usage",
      "timing",
      "events",
    ])
    expect(tabsFor(byId("assistant:req_a"))).toEqual(["summary", "preview", "raw", "source", "events"])
    expect(tabsFor(byId("user:msg_user"))).toEqual(["summary", "preview", "raw", "source", "events"])
  })

  it("adds a diff only to a system update", () => {
    const initial = records.find((row) => row.kind === "system")!
    expect(tabsFor(initial)).toEqual(["systemPrompt", "tools", "source", "events"])
    expect(tabsFor({ ...initial, data: { ...initial.data, before: { system: [], tools: [] } } })[0]).toBe(
      "diff",
    )
  })
})

describe("field availability", () => {
  it("separates pending, not recorded, empty and deleted", () => {
    const open = record({ status: "running", end_seq: null, data: { name: "read" } })
    expect(fieldState(open, "output", true)).toEqual({ state: "pending" })
    expect(fieldState(open, "schema")).toEqual({ state: "not_recorded" })
    const closed = record({ status: "completed", end_seq: "19", data: { output: "" } })
    expect(fieldState(closed, "output", true)).toEqual({ state: "empty" })
    expect(fieldState(closed, "model_output", true)).toEqual({ state: "not_recorded" })
    const deleted = { $payload: { payload_id: "p1", availability: "deleted", reason: "explicitly_deleted" } }
    expect(classifyValue(closed, true, deleted)).toEqual({ state: "deleted", reason: "explicitly_deleted" })
    expect(
      classifyValue(closed, true, { $payload: { payload_id: "p2", availability: "available" } }),
    ).toMatchObject({ state: "payload" })
  })
})

describe("time display", () => {
  const iso = "2026-08-13T14:29:43.983Z"

  it("keeps milliseconds and names the zone", () => {
    expect(formatInstant(iso, "utc", "en-US")).toMatch(/43\.983/)
    expect(formatInstant(iso, "utc", "en-US")).toMatch(/UTC/)
    expect(formatInstant(iso, "unix", "en-US")).toBe(`${Date.UTC(2026, 7, 13, 14, 29, 43) / 1000}.983`)
    expect(formatInstant(null, "local", "en-US")).toBeNull()
  })

  it("formats durations without inventing zero", () => {
    expect(formatDuration(null, "en-US")).toBeNull()
    expect(formatDuration(0, "en-US")).toMatch(/^0\s?ms$/)
    expect(formatDuration(1814, "en-US")).toMatch(/1\.81/)
  })

  it("measures an open interval against the frozen replay clock", () => {
    expect(elapsedUntil("2026-09-11T08:00:00.016Z", "2026-09-11T08:00:00.017Z")).toBe(1)
    expect(elapsedUntil(null, "2026-09-11T08:00:00.017Z")).toBeNull()
  })
})

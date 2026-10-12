import { describe, expect, it } from "vitest"
import { buildFlow } from "./steps"
import { EMPTY_LABELS, type LiteLabels, type LiteStep } from "./types"

const LABELS: LiteLabels = {
  ...EMPTY_LABELS,
  tools: { read: { label: "处理文件", group: "files" }, desktop_publish: { label: "发布到抖音（云电脑）" } },
  groups: { files: { label: "处理文件", collapse: true } },
}

function step(id: string, seq: number, type: string, patch: Partial<LiteStep> = {}): LiteStep {
  return {
    record_id: id,
    kind: type,
    title: id,
    preview: null,
    result_preview: null,
    status: "completed",
    status_reason: null,
    source_session_id: null,
    turn_id: "t",
    run_id: "r",
    generation: null,
    agent_id: "main",
    parent_agent_id: null,
    step_id: null,
    request_id: null,
    call_id: null,
    parent_call_id: null,
    message_id: null,
    part_id: null,
    caused_by_event_id: null,
    start_seq: String(seq),
    end_seq: String(seq),
    as_of_seq: String(seq),
    started_at: null,
    finished_at: null,
    duration_ms: null,
    timing_source: null,
    usage: {},
    step_type: type,
    label: null,
    group: null,
    collapsible: false,
    transparent: false,
    ...patch,
  }
}

const file = (id: string, seq: number, status = "completed") =>
  step(id, seq, "tool", {
    title: "read",
    label: "处理文件",
    group: "files",
    collapsible: status === "completed",
    status,
  })

describe("buildFlow", () => {
  it("orders by seq, drops hidden kinds and counts retries as an annotation", () => {
    const flow = buildFlow(
      [
        step("assistant:1", 9, "assistant", { preview: "回复" }),
        step("request:1", 3, "hidden"),
        step("retry:1", 4, "retry"),
        step("user:1", 1, "user_input"),
        step("step:1", 2, "hidden"),
        step("interrupt:1", 5, "control"),
      ],
      "main",
      LABELS,
    )
    expect(flow.items.map((item) => (item.kind === "step" ? item.step.record_id : item.kind))).toEqual([
      "user:1",
      "assistant:1",
    ])
    expect(flow.retries).toBe(1)
    expect(flow.hidden).toBe(4)
  })

  it("folds runs of collapsible tools of one group, but a failed one breaks the run", () => {
    const flow = buildFlow(
      [
        step("user:1", 1, "user_input"),
        file("tool:a", 2),
        file("tool:b", 3),
        file("tool:c", 4, "failed"),
        file("tool:d", 5),
        step("tool:e", 6, "tool", { title: "desktop_publish", label: "发布到抖音（云电脑）" }),
        file("tool:f", 7),
        file("tool:g", 8),
      ],
      "main",
      LABELS,
    )
    const shape = flow.items.map((item) =>
      item.kind === "group"
        ? `group:${item.label}:${item.steps.length}`
        : item.kind === "step"
          ? item.step.record_id
          : item.kind,
    )
    expect(shape).toEqual(["user:1", "group:处理文件:2", "tool:c", "tool:d", "tool:e", "group:处理文件:2"])
  })

  it("hands a sub-agent its own steps and keeps steps of unknown agents inline", () => {
    const flow = buildFlow(
      [
        step("user:1", 1, "user_input"),
        step("agent:child", 2, "agent", { agent_id: "child", parent_agent_id: "main", title: "排查登录态" }),
        step("tool:x", 3, "tool", { agent_id: "child", title: "desktop_publish" }),
        file("tool:y", 4),
        file("tool:z", 5),
        step("tool:orphan", 6, "tool", { agent_id: "ghost", title: "desktop_publish" }),
        step("assistant:1", 7, "assistant", { preview: "回复" }),
      ].map((item) =>
        item.record_id === "tool:y" || item.record_id === "tool:z" ? { ...item, agent_id: "child" } : item,
      ),
      "main",
      LABELS,
    )
    expect(flow.items.map((item) => item.kind)).toEqual(["step", "agent", "step", "step"])
    const agent = flow.items[1]
    expect(
      agent.kind === "agent" &&
        agent.children.map((child) => (child.kind === "group" ? `group:${child.steps.length}` : child.kind)),
    ).toEqual(["step", "group:2"])
    expect(flow.items[2].kind === "step" && flow.items[2].step.record_id).toBe("tool:orphan")
  })

  it("keeps a gap as a notice in place", () => {
    const flow = buildFlow(
      [
        step("user:1", 1, "user_input"),
        step("gap:1", 2, "gap"),
        step("assistant:1", 3, "assistant", { preview: "回复" }),
      ],
      "main",
      LABELS,
    )
    expect(flow.items.map((item) => item.kind)).toEqual(["step", "gap", "step"])
  })

  it("drops policy-approved permissions and the main agent's own record, keeps real confirmations", () => {
    const flow = buildFlow(
      [
        step("user:1", 1, "user_input"),
        step("agent:main", 2, "agent", { agent_id: "main", title: "agent" }),
        step("permission:auto", 3, "permission", { duration_ms: 0 }),
        step("permission:waited", 4, "permission", { duration_ms: 65_000 }),
        step("permission:open", 5, "permission", { status: "waiting", duration_ms: null }),
        step("assistant:calls", 6, "assistant", { preview: "tool calls" }),
        step("assistant:empty", 7, "assistant", { preview: null }),
        step("assistant:1", 8, "assistant", { preview: "好的" }),
      ],
      "main",
      LABELS,
    )
    expect(flow.items.map((item) => (item.kind === "step" ? item.step.record_id : item.kind))).toEqual([
      "user:1",
      "permission:waited",
      "permission:open",
      "assistant:1",
    ])
  })
})

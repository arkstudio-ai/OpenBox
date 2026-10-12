import type { TFunction } from "i18next"
import { describe, expect, it } from "vitest"
import { conclusion, resolveTool, turnStatusLabel, turnStatusTone } from "./labels"
import type { LiteLabels, TurnSummary } from "./types"

const LABELS: LiteLabels = {
  version: 1,
  tools: {
    desktop_publish: { label: "发布到抖音（云电脑）" },
    video_generate: { label: "生成视频" },
    read: { label: "处理文件", group: "files" },
  },
  prefixes: { "memory.": { label: "查阅记忆", group: "memory" }, mcp_: { label: "调用外部工具" } },
  groups: { files: { label: "处理文件", collapse: true }, memory: { label: "查阅记忆", collapse: true } },
  statuses: {},
  waiting_kinds: {},
}

const t = ((key: string, vars?: Record<string, unknown>) =>
  vars ? `${key} ${JSON.stringify(vars)}` : key) as unknown as TFunction

function turn(patch: Partial<TurnSummary>): TurnSummary {
  return {
    turn_id: "t1",
    session_id: "s1",
    session_title: "会话",
    user_id: "u1",
    workspace_id: "w1",
    started_at: "2026-10-12T06:00:00Z",
    finished_at: "2026-10-12T06:04:00Z",
    last_activity_at: "2026-10-12T06:04:00Z",
    duration_ms: 240_000,
    status: "completed",
    status_reason: null,
    waiting_kind: null,
    user_preview: "发视频",
    assistant_preview: "做好了两版",
    user_message_id: null,
    assistant_message_id: null,
    main_agent_id: "a",
    has_attachments: false,
    tool_counts: {},
    tool_failures: 0,
    agent_count: 0,
    job_count: 0,
    artifact_count: 0,
    retry_count: 0,
    request_count: 1,
    input_tokens: 10,
    output_tokens: 2,
    usage_complete: true,
    first_seq: "1",
    last_seq: "9",
    through_seq: "9",
    ...patch,
  }
}

describe("tool labels", () => {
  it("resolves an exact id, then the longest prefix, then the id itself", () => {
    expect(resolveTool("desktop_publish", LABELS)).toEqual({
      label: "发布到抖音（云电脑）",
      group: null,
      collapse: false,
      known: true,
    })
    expect(resolveTool("read", LABELS)).toMatchObject({ label: "处理文件", group: "files", collapse: true })
    expect(resolveTool("memory.remember", LABELS)).toMatchObject({
      label: "查阅记忆",
      group: "memory",
      collapse: true,
      known: true,
    })
    expect(resolveTool("mcp_call_tool", LABELS)).toMatchObject({
      label: "调用外部工具",
      group: null,
      collapse: false,
    })
    expect(resolveTool("brand_new", LABELS)).toEqual({
      label: "brand_new",
      group: null,
      collapse: false,
      known: false,
    })
    expect(resolveTool("brand_new")).toMatchObject({ known: false })
  })

  it("maps statuses to tones and translation keys without mislabelling unknown values", () => {
    expect(turnStatusTone("failed")).toBe("danger")
    expect(turnStatusTone("waiting")).toBe("accent")
    expect(turnStatusTone("something_else")).toBe("muted")
    expect(turnStatusLabel("completed", t)).toBe("lite.turnStatus.completed")
    expect(turnStatusLabel("something_else", t)).toBe("something_else")
  })
})

describe("the one-line conclusion", () => {
  it("names the failed action and its reason first", () => {
    const failed = turn({
      status: "failed",
      status_reason: "login_expired: creator.douyin.com redirected to /login",
      tool_counts: { video_generate: { total: 1, failed: 0 }, desktop_publish: { total: 1, failed: 1 } },
    })
    expect(conclusion(failed, LABELS, t)).toBe(
      'lite.verdict.failedStep {"action":"发布到抖音（云电脑）","reason":"login_expired: creator.douyin.com redirected to /login"}',
    )
    expect(conclusion(turn({ status: "failed", status_reason: "context overflow" }), LABELS, t)).toBe(
      'lite.verdict.failedReply {"reason":"context overflow"}',
    )
    expect(conclusion(turn({ status: "failed" }), LABELS, t)).toBe("lite.verdict.failedReplyNoReason")
  })

  it("says what the user is being waited on for, with the elapsed time when known", () => {
    const waiting = turn({ status: "waiting", waiting_kind: "permission" })
    const withElapsed = conclusion(waiting, LABELS, t, "6 min")
    expect(withElapsed).toMatch(/^lite\.verdict\.waiting /)
    expect(withElapsed).toContain("lite.waiting.permission")
    expect(withElapsed).toContain('"elapsed":"6 min"')
    expect(conclusion(waiting, LABELS, t)).toMatch(
      /^lite\.verdict\.waitingNoElapsed .*lite\.waiting\.permission/,
    )
    expect(conclusion(turn({ status: "waiting", waiting_kind: null }), LABELS, t)).toContain(
      "lite.waiting.generic",
    )
  })

  it("prefers a key action over the reply text for a completed turn", () => {
    const published = turn({
      tool_counts: { read: { total: 3, failed: 0 }, video_generate: { total: 2, failed: 0 } },
    })
    expect(conclusion(published, LABELS, t)).toBe(
      'lite.verdict.completedAction {"action":"生成视频","count":2}',
    )
    expect(conclusion(turn({ tool_counts: { read: { total: 3, failed: 0 } } }), LABELS, t)).toBe("做好了两版")
    expect(conclusion(turn({ assistant_preview: null }), LABELS, t)).toBe("lite.verdict.completed")
    expect(
      conclusion(
        turn({
          status: "completed_with_errors",
          tool_failures: 2,
          tool_counts: { read: { total: 3, failed: 2 } },
        }),
        LABELS,
        t,
      ),
    ).toBe('lite.verdict.completedWithErrors {"action":"处理文件","count":2}')
  })

  it("clips a long reason", () => {
    const long = "x".repeat(100)
    expect(conclusion(turn({ status: "failed", status_reason: long }), LABELS, t)).toBe(
      `lite.verdict.failedReply {"reason":"${"x".repeat(60)}…"}`,
    )
    expect(conclusion(turn({ status: "running" }), LABELS, t)).toBe("lite.verdict.running")
    expect(conclusion(turn({ status: "weird" }), LABELS, t)).toBe("weird")
  })
})

describe("plainPreview", () => {
  it("strips Markdown decoration and collapses whitespace", async () => {
    const { plainPreview } = await import("./text")
    expect(plainPreview("### 标题\n- **加粗** 和 `code`\n> 引用 [链接](http://x)")).toBe(
      "标题 加粗 和 code 引用 链接",
    )
    expect(plainPreview("  ")).toBeNull()
    expect(plainPreview(null)).toBeNull()
  })
})

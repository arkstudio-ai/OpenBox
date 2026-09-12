// Event logs for the trajectory fixture: the backend's golden sessions exactly
// as shipped, a hand-authored session exercising the inspector's typed views
// and availability states, a list population for filters and paging, and a
// generated 100,000-event session for scale evidence. Each is plain protocol
// v1 events; the fake server projects them with the real reducer.
import { readFileSync } from "node:fs"
import { deflateSync } from "node:zlib"
import type {
  AgentSummary,
  ProjectionState,
  TrajectoryEvent,
  TrajectoryStatistics,
} from "../../src/features/admin-trajectories/types/protocol"
import type { FixtureSessionSpec, SessionMeta } from "./trajectory-server"

/* ------------------------------ golden files ----------------------------- */

export interface GoldenFixture {
  name: string
  events: TrajectoryEvent[]
  expected_state: ProjectionState
  expected_statistics: TrajectoryStatistics
  expected_agents: AgentSummary[]
  historical: { through_seq: string; state: ProjectionState }
}

/** backend/trajectory/fixtures — read-only, shared with the Python projector tests. */
export function loadGolden(file: "session_v1.json" | "edge_cases_v1.json"): GoldenFixture {
  return JSON.parse(
    readFileSync(new URL(`../../../backend/trajectory/fixtures/${file}`, import.meta.url), "utf8"),
  ) as GoldenFixture
}

function meta(sessionId: string, patch: Partial<SessionMeta> & Pick<SessionMeta, "owner">): SessionMeta {
  return {
    session_id: sessionId,
    title: null,
    workspace: { id: "ws-research", name: "Research" },
    running_status: "idle",
    recording_status: "recording",
    last_activity_at: "2026-09-11T08:00:00.000Z",
    model: null,
    agent: null,
    recorded: true,
    ...patch,
  }
}

export function goldenSpec(fixture: GoldenFixture, title: string): FixtureSessionSpec {
  const first = fixture.events[0]
  return {
    meta: meta(first.session_id, {
      title,
      owner: {
        user_id: first.user_id,
        username: `golden-${first.user_id}`,
        email: `${first.user_id}@example.test`,
      },
      last_activity_at: fixture.events[fixture.events.length - 1].occurred_at,
      model: "fixture",
      agent: first.agent_id ?? null,
    }),
    events: fixture.events,
  }
}

/* --------------------------------- media --------------------------------- */

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n
  for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
  return c >>> 0
})

function crc32(bytes: Buffer): number {
  let c = 0xffffffff
  for (const byte of bytes) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function pngChunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, "ascii"), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([length, body, crc])
}

/** A gradient PNG large enough to see in screenshots and to tell apart from a broken image. */
export function fixturePng(width = 240, height = 120): Buffer {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header[8] = 8 // bit depth
  header[9] = 2 // truecolour RGB
  const stride = width * 3 + 1
  const raw = Buffer.alloc(stride * height)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = y * stride + 1 + x * 3
      raw[offset] = 40 + Math.round((x / width) * 180)
      raw[offset + 1] = 90 + Math.round((y / height) * 120)
      raw[offset + 2] = 190
    }
  }
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
  return Buffer.concat([
    signature,
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(raw)),
    pngChunk("IEND", Buffer.alloc(0)),
  ])
}

/* ------------------------------- event logs ------------------------------ */

type Ids = Partial<
  Pick<
    TrajectoryEvent,
    | "turn_id"
    | "run_id"
    | "step_id"
    | "request_id"
    | "call_id"
    | "agent_id"
    | "parent_agent_id"
    | "message_id"
    | "part_id"
    | "source_session_id"
  >
>

export class EventLog {
  readonly events: TrajectoryEvent[] = []
  private clock: number

  constructor(
    readonly sessionId: string,
    readonly ownerId: string,
    start: string,
  ) {
    this.clock = Date.parse(start)
  }

  /** Append one committed event `gapMs` after the previous one; returns its seq. */
  push(type: string, ids: Ids, data: Record<string, unknown> = {}, gapMs = 40): number {
    this.clock += gapMs
    const seq = this.events.length + 1
    const at = new Date(this.clock).toISOString()
    this.events.push({
      event_id: `evt_${this.sessionId}_${seq}`,
      trajectory_id: `trj_${this.sessionId}`,
      user_id: this.ownerId,
      session_id: this.sessionId,
      source_session_id: this.sessionId,
      seq: String(seq),
      type,
      version: 1,
      occurred_at: at,
      recorded_at: at,
      ...ids,
      data,
    })
    return seq
  }

  get lastAt(): string {
    return this.events.length
      ? this.events[this.events.length - 1].occurred_at
      : new Date(this.clock).toISOString()
  }
}

/* ------------------------------ rich session ----------------------------- */

export const RICH_SESSION = "sess-rich-report"
export const RICH_OWNER = { user_id: "user-mei", username: "mei", email: "mei@example.test" }

const BASH_TOOL = {
  name: "bash",
  description: "Run a shell command in the workspace sandbox.",
  parameters: {
    type: "object",
    properties: {
      command: { type: "string", description: "Command line to run" },
      timeout: { type: "integer", description: "Seconds before the command is stopped", default: 120 },
    },
    required: ["command"],
  },
}

const WRITE_TOOL = {
  name: "write",
  description: "Create or overwrite a file.",
  parameters: {
    type: "object",
    properties: {
      path: { type: "string", description: "Workspace-relative path" },
      content: { type: "string" },
    },
    required: ["path", "content"],
  },
}

export const REPORT_TEXT = "# Build report\n\nTests fail in parser.spec.ts.\n"
export const WRITE_RESULT = { path: "report.md", bytes_written: 45, lines: 3 }
export const FINAL_REVIEW = "The report matches the failing test."

export interface RichFixture {
  spec: FixtureSessionSpec
  /** Named positions in the log, so tests never hard-code sequence numbers. */
  seq: Readonly<Record<RichMark, number>>
  /** Events committed while a test is watching (question answered, run finished). */
  liveTail: TrajectoryEvent[]
  png: Buffer
}

export type RichMark =
  | "accepted"
  | "prepared"
  | "firstDelta"
  | "usage"
  | "requestFinished"
  | "bashRequested"
  | "bashDenied"
  | "writeRequested"
  | "writeStarted"
  | "artifact"
  | "writeFinished"
  | "step2"
  | "reviewerSpawned"
  | "reviewerUsage"
  | "reviewerFinished"
  | "question"
  | "pendingPermission"
  | "head"
  | "questionResolved"
  | "liveHead"

export function richFixture(): RichFixture {
  const log = new EventLog(RICH_SESSION, RICH_OWNER.user_id, "2026-09-11T09:00:00.000Z")
  const main = { turn_id: "turn_1", run_id: "run_1", agent_id: "agent_main" }
  const step1 = { ...main, step_id: "step_1" }
  const step2 = { ...main, step_id: "step_2" }
  const reviewer = { ...step2, agent_id: "agent_reviewer", parent_agent_id: "agent_main" }
  const png = fixturePng()
  const writeResult = Buffer.from(JSON.stringify(WRITE_RESULT))
  const seq = {} as Record<RichMark, number>

  log.push("trajectory.started", main, { existing_session: false })
  seq.accepted = log.push(
    "input.accepted",
    { ...main, message_id: "msg_rich_1" },
    {
      text: "请检查构建失败的截图，并把结论写进 **report.md**。",
      source_kind: "web",
      client_channel: "web",
      actor: "owner",
      attachments: [{ name: "build-error.png", media_type: "image/png", size_bytes: png.length }],
    },
  )
  log.push("turn.started", main)
  log.push("run.started", main, { origin: "user_input", generation: 1 })
  log.push("step.started", step1)
  seq.prepared = log.push(
    "request.prepared",
    { ...step1, request_id: "req_1" },
    {
      provider: "fixture-provider",
      model: "fixture-large",
      purpose: "chat",
      capture_level: "adapter_input",
      input: {
        model: "fixture-large",
        stream: true,
        temperature: 0.2,
        max_tokens: 4096,
        tool_choice: "auto",
        system: "You are the OpenBox agent. Work carefully and explain each change.",
        messages: [
          {
            role: "user",
            content: [
              { type: "input_text", text: "请检查构建失败的截图，并把结论写进 report.md。" },
              {
                type: "input_image",
                image: {
                  $media: {
                    payload_id: "pl_img_ok",
                    sha256: "fixture-sha-img",
                    media_type: "image/png",
                    size_bytes: png.length,
                    availability: "available",
                  },
                  source_asset_id: "asset_build_png",
                  source_kind: "attachment",
                  original_encoding: "base64",
                  declared_media_type: "image/png",
                },
              },
              {
                type: "input_image",
                image: {
                  $media: { availability: "deleted", reason: "Attachment deleted by its owner" },
                  source_asset_id: "asset_old_jpeg",
                  source_kind: "attachment",
                  original_encoding: "base64",
                  declared_media_type: "image/jpeg",
                },
              },
              {
                type: "input_image",
                image: {
                  $media: { availability: "not_recorded" },
                  source_kind: "remote_url",
                  original_encoding: "url",
                  declared_media_type: "image/webp",
                },
              },
            ],
          },
        ],
        tools: [BASH_TOOL, WRITE_TOOL],
      },
    },
  )
  log.push("request.started", { ...step1, request_id: "req_1" }, {}, 120)
  seq.firstDelta = log.push(
    "request.delta",
    { ...step1, request_id: "req_1" },
    {
      chunk_index: 0,
      blocks: [
        { block_id: "reasoning:0", type: "reasoning", delta: "截图显示单元测试失败，" },
        { block_id: "text:0", type: "text", delta: "我先运行测试确认失败原因" },
      ],
    },
    380,
  )
  log.push(
    "request.delta",
    { ...step1, request_id: "req_1" },
    {
      chunk_index: 1,
      blocks: [
        { block_id: "text:0", type: "text", delta: "，然后更新报告。" },
        {
          block_id: "tool:call_bash",
          type: "tool_arguments",
          delta: '{"command": "rm -rf build && npm test"}',
          name: "bash",
          call_id: "call_bash",
        },
        {
          block_id: "tool:call_write",
          type: "tool_arguments",
          delta: '{"path": "report.md"}',
          name: "write",
          call_id: "call_write",
        },
      ],
    },
    210,
  )
  seq.usage = log.push(
    "request.usage",
    { ...step1, request_id: "req_1" },
    {
      mode: "replace",
      usage: { input_tokens: 1200, cache_read_input_tokens: 300, output_tokens: 85, reasoning_tokens: 20 },
    },
  )
  seq.requestFinished = log.push(
    "request.finished",
    { ...step1, request_id: "req_1" },
    { status: "completed", finish_reason: "tool_calls", duration_ms: 950 },
  )
  seq.bashRequested = log.push(
    "tool.requested",
    { ...step1, request_id: "req_1", call_id: "call_bash" },
    {
      name: "bash",
      title: "Run tests",
      arguments_raw: '{"command": "rm -rf build && npm test"}',
      requested_arguments: { command: "rm -rf build && npm test" },
      schema: BASH_TOOL,
      schema_source: "provider_request",
      provider_call_id: "toolu_fixture_bash",
    },
  )
  log.push(
    "permission.requested",
    { ...step1, call_id: "call_bash" },
    {
      permission_id: "perm_bash",
      permission: "bash",
      patterns: ["rm -rf *"],
      reason: "Command deletes files",
    },
  )
  log.push(
    "permission.resolved",
    { ...step1, call_id: "call_bash" },
    {
      permission_id: "perm_bash",
      status: "denied",
      decision: "deny",
      source_kind: "user",
      actor: "owner",
      reason: "Owner refused to delete build output",
    },
    2400,
  )
  seq.bashDenied = log.push(
    "tool.finished",
    { ...step1, call_id: "call_bash" },
    { status: "denied", error: "Permission denied: rm -rf build" },
  )
  seq.writeRequested = log.push(
    "tool.requested",
    { ...step1, request_id: "req_1", call_id: "call_write" },
    {
      name: "write",
      title: "Write report.md",
      requested_arguments: { path: "report.md", content: REPORT_TEXT },
      schema: WRITE_TOOL,
      schema_source: "provider_request",
    },
  )
  seq.writeStarted = log.push(
    "tool.started",
    { ...step1, call_id: "call_write" },
    { effective_arguments: { path: "report.md", content: REPORT_TEXT } },
  )
  log.push(
    "tool.output",
    { ...step1, call_id: "call_write" },
    { chunk_index: 0, mode: "delta", output: "Writing report.md…" },
    15,
  )
  seq.artifact = log.push(
    "artifact.recorded",
    { ...step1, call_id: "call_write" },
    {
      artifact_id: "art_report",
      artifact_type: "file_diff",
      name: "report.md",
      path: "report.md",
      operation: "create",
      capture_level: "executor_content",
      before: { availability: "absent" },
      after: {
        availability: "available",
        text: REPORT_TEXT,
        sha256: "fixture-sha-report",
        size_bytes: REPORT_TEXT.length,
        source: "executor_content",
      },
      diff: "--- /dev/null\n+++ b/report.md\n@@ -0,0 +1,3 @@\n+# Build report\n+\n+Tests fail in parser.spec.ts.\n",
    },
    12,
  )
  seq.writeFinished = log.push(
    "tool.finished",
    { ...step1, call_id: "call_write" },
    {
      status: "completed",
      output: {
        $payload: {
          payload_id: "pl_write_result",
          sha256: "fixture-sha-result",
          size_bytes: writeResult.length,
          media_type: "application/json",
          availability: "available",
        },
      },
      model_output: "Wrote report.md (3 lines).",
      duration_ms: 42,
    },
    15,
  )
  log.push("step.finished", step1, { status: "completed" })
  seq.step2 = log.push("step.started", step2)
  seq.reviewerSpawned = log.push("agent.spawned", reviewer, {
    name: "Reviewer",
    prompt: "Review report.md for accuracy",
    configuration: { model: "fixture-small" },
  })
  log.push(
    "request.prepared",
    { ...reviewer, request_id: "req_review" },
    {
      provider: "fixture-provider",
      model: "fixture-small",
      purpose: "subagent",
      capture_level: "provider_wire",
      input: { model: "fixture-small", messages: [{ role: "user", content: "Review report.md" }] },
    },
  )
  log.push("request.started", { ...reviewer, request_id: "req_review" })
  log.push(
    "request.delta",
    { ...reviewer, request_id: "req_review" },
    { chunk_index: 0, blocks: [{ block_id: "text:0", type: "text", delta: FINAL_REVIEW }] },
    300,
  )
  seq.reviewerUsage = log.push(
    "request.usage",
    { ...reviewer, request_id: "req_review" },
    { mode: "replace", usage: { input_tokens: 300, output_tokens: 12 } },
  )
  log.push(
    "request.finished",
    { ...reviewer, request_id: "req_review" },
    { status: "completed", finish_reason: "stop" },
  )
  seq.reviewerFinished = log.push("agent.finished", reviewer, { status: "completed", output: FINAL_REVIEW })
  seq.question = log.push("question.asked", step2, {
    question_id: "q_commit",
    questions: [
      {
        header: "Commit",
        question: "要把 report.md 提交到仓库吗？",
        options: [{ label: "提交", description: "创建一次提交" }, { label: "暂不提交" }],
        multiple: false,
      },
    ],
    expires_at: "2026-09-11T10:00:00.000Z",
  })
  seq.pendingPermission = log.push("permission.requested", step2, {
    permission_id: "perm_push",
    permission: "git push",
    patterns: ["git push origin main"],
    reason: "Push the report to the remote",
  })
  seq.head = seq.pendingPermission
  // Committed later, while a test watches.
  seq.questionResolved = log.push(
    "question.resolved",
    step2,
    { question_id: "q_commit", answers: [["提交"]], source_kind: "user" },
    5000,
  )
  log.push(
    "permission.expired",
    step2,
    { permission_id: "perm_push", reason: "No decision before the timeout" },
    1000,
  )
  log.push("run.finished", main, { status: "completed" })
  seq.liveHead = log.push("turn.finished", main, { status: "completed" })

  return {
    spec: {
      meta: meta(RICH_SESSION, {
        title: "修复构建报告",
        owner: RICH_OWNER,
        workspace: { id: "ws-docs", name: "Docs" },
        running_status: "waiting",
        last_activity_at: log.events[seq.head - 1].occurred_at,
        model: "fixture-large",
        agent: "agent_main",
      }),
      events: log.events.slice(0, seq.head),
      payloads: [
        { id: "pl_img_ok", body: png, mediaType: "image/png", firstSeq: seq.prepared },
        {
          id: "pl_write_result",
          body: writeResult,
          mediaType: "application/json",
          firstSeq: seq.writeFinished,
        },
      ],
    },
    seq,
    liveTail: log.events.slice(seq.head),
    png,
  }
}

/* ----------------------------- list population --------------------------- */

const LIST_OWNERS = [
  { user_id: "user-alice", username: "alice", email: "alice@example.test" },
  { user_id: "user-bob", username: "bob", email: "bob@corp.example" },
  RICH_OWNER,
]
const RUN_STATES = ["idle", "running", "waiting", "error"]

/** Enough short sessions for two list pages; titles and owners vary for filtering. */
export function listPopulation(count = 56): FixtureSessionSpec[] {
  return Array.from({ length: count }, (_, index) => {
    const sessionId = `sess-list-${String(index).padStart(3, "0")}`
    const owner = LIST_OWNERS[index % LIST_OWNERS.length]
    const log = new EventLog(
      sessionId,
      owner.user_id,
      new Date(Date.parse("2026-09-10T00:00:00.000Z") + index * 3_600_000).toISOString(),
    )
    const ids = { turn_id: `turn_${index}`, run_id: `run_${index}`, agent_id: "agent_main" }
    log.push("trajectory.started", ids, { existing_session: index % 5 === 0 })
    log.push(
      "input.accepted",
      { ...ids, message_id: `msg_${index}` },
      { text: `Task ${index}: tidy the changelog`, source_kind: "web" },
    )
    log.push("turn.started", ids)
    return {
      meta: meta(sessionId, {
        title: `Changelog task ${index}`,
        owner,
        workspace: index % 2 ? { id: "ws-docs", name: "Docs" } : { id: "ws-research", name: "Research" },
        running_status: RUN_STATES[index % RUN_STATES.length],
        recording_status: index % 7 === 3 ? "gap" : "recording",
        last_activity_at: log.lastAt,
        model: "fixture-small",
        agent: "agent_main",
      }),
      events: log.events,
    }
  })
}

export function unrecordedSpec(): FixtureSessionSpec {
  return {
    meta: meta("sess-never-recorded", {
      title: "Never recorded chat",
      owner: LIST_OWNERS[1],
      recorded: false,
      recording_status: "not_recorded",
      last_activity_at: "2026-09-09T12:00:00.000Z",
    }),
    events: [],
  }
}

/* ------------------------------- scale session ---------------------------- */

export const SCALE_SESSION = "sess-scale-100k"

export interface ScaleFixture {
  spec: FixtureSessionSpec
  eventCount: number
  toolRecords: number
  recordCount: number
}

/**
 * 100,000 events forming 10,000 tool calls plus one turn, run and step. The
 * projector makes 10,004 records: the tools, the turn, run and step, and the
 * trajectory's baseline record (from `trajectory.started`). Each call:
 * requested, started, outputs, finished; the last call has 3 outputs instead
 * of 7 so the total is exactly 100,000.
 */
const SCALE_RUN = { turn_id: "turn_scale", run_id: "run_scale", agent_id: "agent_main" }
const SCALE_STEP = { ...SCALE_RUN, step_id: "step_scale" }

/** One tool call: requested, started, `outputs` chunks, finished (outputs + 3 events). */
function pushScaleTool(log: EventLog, index: number, outputs: number): void {
  const ids = { ...SCALE_STEP, call_id: `call_${String(index).padStart(5, "0")}` }
  const failed = index % 97 === 0
  log.push(
    "tool.requested",
    ids,
    {
      name: index % 4 ? "read" : "bash",
      title: `Inspect module ${index}`,
      requested_arguments: { path: `src/modules/module_${index}.ts` },
    },
    index % 500 === 0 ? 30_000 : 20,
  )
  log.push("tool.started", ids, {}, 5)
  for (let chunk = 0; chunk < outputs; chunk += 1) {
    log.push(
      "tool.output",
      ids,
      { chunk_index: chunk, mode: "delta", output: `line ${chunk + 1} of module ${index}\n` },
      3,
    )
  }
  const duration = 20 + (index % 50)
  log.push(
    "tool.finished",
    ids,
    failed
      ? { status: "failed", error: "exit code 1", duration_ms: duration }
      : { status: "completed", duration_ms: duration },
    4,
  )
}

/** Ten more committed events (one tool call) continuing the scale log after `afterSeq`, for live-output samples. */
export function scaleToolEvents(afterSeq: number, index: number, afterTime: string): TrajectoryEvent[] {
  const log = new EventLog(SCALE_SESSION, "user-scale", afterTime)
  pushScaleTool(log, index, 7)
  return log.events.map((event, offset) => ({
    ...event,
    seq: String(afterSeq + offset + 1),
    event_id: `evt_${SCALE_SESSION}_${afterSeq + offset + 1}`,
  }))
}

export function scaleFixture(tools = 10_000): ScaleFixture {
  const log = new EventLog(SCALE_SESSION, "user-scale", "2026-09-11T00:00:00.000Z")
  log.push("trajectory.started", SCALE_RUN, { existing_session: false }, 0)
  log.push("turn.started", SCALE_RUN, {}, 1)
  log.push("run.started", SCALE_RUN, {}, 1)
  log.push("step.started", SCALE_STEP, {}, 1)
  for (let index = 0; index < tools; index += 1) pushScaleTool(log, index, index === tools - 1 ? 3 : 7)
  return {
    spec: {
      meta: meta(SCALE_SESSION, {
        title: "Scale: 100k events",
        owner: { user_id: "user-scale", username: "scale", email: "scale@example.test" },
        running_status: "idle",
        last_activity_at: log.lastAt,
        model: "fixture-large",
        agent: "agent_main",
      }),
      events: log.events,
      checkpoints: true,
    },
    eventCount: log.events.length,
    toolRecords: tools,
    // Tool records plus baseline, turn, run and step.
    recordCount: tools + 4,
  }
}

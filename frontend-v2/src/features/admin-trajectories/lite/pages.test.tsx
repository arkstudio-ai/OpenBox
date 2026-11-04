import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter, Route, Routes, useLocation } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import type { AuthUser } from "@/shared/types/api"
import { useTrajectoryAccess } from "../stores/access"
import { TrajectoryReplayPage } from "./components/ReplayPage"
import { TrajectoryTurnPage } from "./components/TurnPage"
import { TrajectoryUserTurnsPage } from "./components/UserTurnsPage"
import { TrajectoryUsersPage } from "./components/UsersPage"
import type {
  LiteLabels,
  LiteStep,
  LiteUserRow,
  MediaPrompt,
  SessionReplay,
  TurnDetail,
  TurnSummary,
} from "./types"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

const get = vi.fn<(path: string) => Promise<unknown>>()
vi.mock("@/shared/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/http")>()
  return { ...actual, http: { ...actual.http, get: (path: string) => get(path) } }
})

const LABELS: LiteLabels = {
  version: 1,
  tools: {
    desktop_publish: { label: "发布到抖音（云电脑）" },
    video_generate: { label: "生成视频" },
    read: { label: "处理文件", group: "files" },
  },
  prefixes: {},
  groups: { files: { label: "处理文件", collapse: true } },
  statuses: {},
  waiting_kinds: {},
}

const USER_A: LiteUserRow = {
  user: { user_id: "user_a", username: "张店长", email: "zhang@example.test" },
  last_activity_at: new Date(Date.now() - 3 * 60_000).toISOString(),
  current_status: "waiting",
  turns: 14,
  failed_turns: 3,
  waiting_turns: 1,
  running_turns: 0,
  sessions: 4,
  input_tokens: 182_000,
  output_tokens: 41_000,
  usage_complete: true,
  credits: "12.5",
}
const USER_B: LiteUserRow = {
  user: { user_id: "user_b", username: null, email: null },
  last_activity_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
  current_status: "completed",
  turns: 2,
  failed_turns: 0,
  waiting_turns: 0,
  running_turns: 0,
  sessions: 1,
  input_tokens: null,
  output_tokens: null,
  usage_complete: false,
}

function turn(patch: Partial<TurnSummary>): TurnSummary {
  return {
    turn_id: "turn_1",
    session_id: "session_1",
    session_title: "国庆活动预热视频",
    user_id: "user_a",
    workspace_id: "ws",
    started_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    last_activity_at: new Date().toISOString(),
    duration_ms: 252_000,
    status: "failed",
    status_reason: "login_expired",
    waiting_kind: null,
    user_preview: "把刚才那条火锅视频发到抖音",
    assistant_preview: "视频已生成，但发布时发现抖音登录已过期",
    user_message_id: "m1",
    assistant_message_id: "m2",
    main_agent_id: "main",
    has_attachments: true,
    tool_counts: {
      video_generate: { total: 1, failed: 0 },
      desktop_publish: { total: 1, failed: 1 },
      read: { total: 6, failed: 0 },
    },
    tool_failures: 1,
    agent_count: 1,
    job_count: 0,
    artifact_count: 0,
    retry_count: 1,
    request_count: 3,
    input_tokens: 18_200,
    output_tokens: 2_900,
    usage_complete: true,
    credits: "3.2",
    first_seq: "1",
    last_seq: "40",
    through_seq: "40",
    ...patch,
  }
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
    turn_id: "turn_1",
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
    duration_ms: 1200,
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

const FULL_PROMPT = "镜头一：火锅店门口，暖光推近，蒸汽升起。\n镜头二：服务员端上锅底，特写红油翻滚。"

const PROMPT: MediaPrompt = {
  record_id: "tool:gen",
  tool_id: "video_generate",
  medium: "video",
  agent_id: "child",
  status: "completed",
  status_reason: null,
  started_at: new Date().toISOString(),
  shot: 2,
  prompt: FULL_PROMPT,
  settings: { resolution: "1080p", duration: 8, generate_audio: false },
  inputs: [{ asset_id: "ast_1", role: "first_frame" }],
  result_preview: "job_1 submitted",
}

const DETAIL: TurnDetail = {
  summary: turn({}),
  through_seq: "40",
  labels_version: 1,
  media_prompts: [PROMPT],
  steps: [
    step("user:m1", 1, "user_input", { preview: "把刚才那条火锅视频发到抖音" }),
    step("request:1", 2, "hidden"),
    step("assistant:r1", 3, "assistant", { preview: "好的，我先确认账号" }),
    step("tool:read1", 4, "tool", { title: "read", label: "处理文件", group: "files", collapsible: true }),
    step("tool:read2", 5, "tool", { title: "read", label: "处理文件", group: "files", collapsible: true }),
    step("tool:gen", 6, "tool", { title: "video_generate", label: "生成视频", preview: '{"prompt":"国庆"}' }),
    step("tool:pub", 7, "tool", {
      title: "desktop_publish",
      label: "发布到抖音（云电脑）",
      status: "failed",
      status_reason: "login_expired: redirected to /login",
    }),
    step("retry:1", 8, "retry"),
    step("agent:child", 9, "agent", {
      agent_id: "child",
      title: "排查登录态",
      result_preview: "cookie 已失效",
    }),
    step("tool:child1", 10, "tool", { agent_id: "child", title: "desktop_login", label: "云电脑登录" }),
    step("assistant:r2", 11, "assistant", { preview: "抖音登录已过期，请扫码" }),
  ],
}

function answer(path: string): Promise<unknown> {
  if (path.endsWith("/lite/labels")) return Promise.resolve(LABELS)
  if (path.includes("/users/user_a/turns")) {
    const url = new URL(path, "http://x")
    const statuses = url.searchParams.getAll("status")
    const items =
      statuses.length && !statuses.includes("failed")
        ? []
        : [
            turn({}),
            turn({
              turn_id: "turn_0",
              status: "completed",
              status_reason: null,
              session_id: "session_0",
              session_title: "门店日常运营",
            }),
          ]
    return Promise.resolve({
      items: url.searchParams.get("limit") === "1" ? items.slice(0, 1) : items,
      next_cursor: null,
      has_more: false,
    })
  }
  if (path.includes("/users?") || path.endsWith("/users")) {
    const url = new URL(path, "http://x")
    const q = url.searchParams.get("q")
    const items =
      q === "user_a"
        ? [USER_A]
        : url.searchParams.get("has_failures") === "true"
          ? [USER_A]
          : [USER_A, USER_B]
    return Promise.resolve({
      items,
      next_cursor: url.searchParams.get("cursor") ? null : "cursorB",
      has_more: !url.searchParams.get("cursor"),
    })
  }
  if (path.includes("/sessions/session_1/replay")) {
    const url = new URL(path, "http://x")
    const later = url.searchParams.get("cursor") === "c2"
    const page: SessionReplay = {
      session: {
        session_id: "session_1",
        title: "国庆活动预热视频",
        user_id: "user_a",
        workspace_id: "ws",
        username: "张店长",
        email: null,
      },
      total_turns: 2,
      through_seq: "40",
      next_cursor: later ? null : "c2",
      has_more: !later,
      items: later
        ? [
            {
              summary: turn({ turn_id: "turn_2", status: "completed", status_reason: null }),
              events: [
                {
                  type: "user",
                  record_id: "user:m3",
                  at: null,
                  status: "accepted",
                  text: "再来一条",
                  attachments: false,
                  source_kind: null,
                },
                {
                  type: "reply",
                  record_id: "assistant:r9",
                  at: null,
                  status: "completed",
                  text: "好的",
                  status_reason: null,
                },
              ],
            },
          ]
        : [
            {
              summary: turn({}),
              events: [
                {
                  type: "user",
                  record_id: "user:m1",
                  at: null,
                  status: "accepted",
                  text: "把刚才那条火锅视频发到抖音",
                  attachments: true,
                  source_kind: "web",
                },
                {
                  type: "reply",
                  record_id: "assistant:r1",
                  at: null,
                  status: "completed",
                  text: "**好的**，先出方案",
                  status_reason: null,
                },
                { ...PROMPT, type: "media_prompt", at: null },
                {
                  type: "question",
                  record_id: "question:q1",
                  at: null,
                  status: "completed",
                  questions: [{ question: "用哪个方案？" }],
                  answers: ["方案 A"],
                },
                {
                  type: "reply",
                  record_id: "assistant:r2",
                  at: null,
                  status: "failed",
                  text: null,
                  status_reason: "overloaded",
                },
              ],
            },
          ],
    }
    return Promise.resolve(page)
  }
  if (path.includes("/turns/turn_1")) return Promise.resolve(DETAIL)
  if (path.includes("/turns/missing"))
    return Promise.reject(Object.assign(new Error("Turn not found"), { status: 404 }))
  if (path.includes("/records/")) {
    const id = decodeURIComponent(path.split("/records/")[1]!.split("?")[0]!)
    const record = DETAIL.steps.find((item) => item.record_id === id)!
    const data =
      id === "tool:pub"
        ? { requested_arguments: { title: "国庆来张记" }, model_output: "redirected to /login" }
        : id === "user:m1"
          ? { text: "把刚才那条火锅视频发到抖音", attachments: [{ $media: { payload_id: "p" } }] }
          : {}
    const blocks = id.startsWith("assistant")
      ? [
          { block_id: "r", type: "reasoning", text: "想一想" },
          { block_id: "t", type: "text", text: "**好的**，我先确认账号" },
        ]
      : []
    return Promise.resolve({ record: { ...record, data, blocks }, through_seq: "40", projector_version: 1 })
  }
  return Promise.reject(new Error(`unexpected ${path}`))
}

function Location() {
  const { pathname, search } = useLocation()
  return <output data-testid="location">{pathname + search}</output>
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/app/admin/trajectories" element={<TrajectoryUsersPage />} />
          <Route
            path="/app/admin/trajectories/users/:userId"
            element={<TrajectoryUserTurnsPage userId="user_a" />}
          />
          <Route
            path="/app/admin/trajectories/users/:userId/turns/:sessionId/:turnId"
            element={<TrajectoryTurnPage userId="user_a" sessionId="session_1" turnId="turn_1" />}
          />
          <Route
            path="/app/admin/trajectories/users/:userId/sessions/:sessionId"
            element={<TrajectoryReplayPage userId="user_a" sessionId="session_1" />}
          />
          <Route path="*" element={null} />
        </Routes>
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  get.mockReset()
  get.mockImplementation(answer)
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  useAuthStore.setState({
    user: { id: "admin_viewer", role: "admin" } as unknown as AuthUser,
    isAuthenticated: true,
    isLoading: false,
  })
})

afterEach(() => {
  cleanup()
})

describe("用户列表", () => {
  it("lists users with their verdicts, filters by failures through the URL and pages", async () => {
    renderAt("/app/admin/trajectories")
    expect(await screen.findByText("张店长")).toBeTruthy()
    expect(screen.getByText("zhang@example.test")).toBeTruthy()
    expect(screen.getAllByText("user_b").length).toBeGreaterThan(0)
    expect(screen.getByText("lite.turnStatus.waiting")).toBeTruthy()
    expect(screen.getByText("3")).toBeTruthy()
    expect(screen.getByText("12.5")).toBeTruthy()
    expect(screen.getAllByText("lite.credits.none").length).toBeGreaterThan(0)
    // The default window is 7 days; the request carries its start.
    const first = get.mock.calls.find(([path]) => path.includes("/users?"))![0]
    expect(first).toContain("activity_from=")
    expect(first).toContain("limit=50")
    fireEvent.click(screen.getByLabelText("lite.users.onlyFailures"))
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/app/admin/trajectories?failures=1"),
    )
    await waitFor(() => expect(screen.queryAllByText("user_b")).toHaveLength(0))
    fireEvent.click(screen.getByRole("button", { name: "list.next" }))
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/app/admin/trajectories?failures=1&cursor=cursorB&trail=.",
      ),
    )
    const open = await screen.findAllByRole("link", { name: "lite.users.open" })
    expect(open[0]!.getAttribute("href")).toBe("/app/admin/trajectories/users/user_a")
    expect(screen.getByTestId("trajectory-mode-toggle")).toBeTruthy()
  })

  it("stops reading once access is refused", async () => {
    useTrajectoryAccess.setState({ epoch: 1, denied: { reason: "forbidden", status: 403 } })
    renderAt("/app/admin/trajectories")
    expect(await screen.findByText("access.forbidden")).toBeTruthy()
    expect(get).not.toHaveBeenCalled()
  })
})

describe("某用户的对话轮", () => {
  it("groups turns by session, names the tools in words and filters through the URL", async () => {
    renderAt("/app/admin/trajectories/users/user_a")
    const previews = await screen.findAllByText("把刚才那条火锅视频发到抖音")
    expect(previews).toHaveLength(2)
    expect(screen.getAllByText("张店长").length).toBeGreaterThan(0)
    expect(screen.getByText("国庆活动预热视频")).toBeTruthy()
    expect(screen.getByText("门店日常运营")).toBeTruthy()
    const row = previews[0]!.closest("tr")!
    expect(row.getAttribute("data-status")).toBe("failed")
    // Failed tools sort first and read as actions, not ids.
    // Tool names arrive with the dictionary; until then the chip shows the id.
    await waitFor(() => {
      const chips = within(row)
        .getAllByTitle(/^(desktop_publish|video_generate|read)$/)
        .map((node) => node.textContent)
      expect(chips[0]).toContain('"label":"发布到抖音（云电脑）"')
      expect(chips).toHaveLength(3)
    })
    expect(within(row).getByText("lite.turns.attachments")).toBeTruthy()
    const turnLink = within(row).getByRole("link")
    expect(turnLink.getAttribute("href")).toBe("/app/admin/trajectories/users/user_a/turns/session_1/turn_1")
    fireEvent.click(screen.getByRole("button", { name: "lite.turns.abnormalOnly" }))
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toContain(
        "status=failed%2Ccompleted_with_errors%2Cwaiting%2Cunknown",
      ),
    )
    fireEvent.click(screen.getByRole("button", { name: "lite.turnStatus.completed" }))
    await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("completed"))
    fireEvent.click(screen.getByRole("button", { name: "lite.turnStatus.failed" }))
    fireEvent.click(screen.getByRole("button", { name: "lite.turnStatus.waiting" }))
    fireEvent.click(screen.getByRole("button", { name: "lite.turnStatus.unknown" }))
    fireEvent.click(screen.getByRole("button", { name: "lite.turnStatus.completed_with_errors" }))
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/app/admin/trajectories/users/user_a?status=completed",
      ),
    )
    expect(await screen.findByText("lite.turns.emptyFiltered")).toBeTruthy()
  })
})

describe("轮详情", () => {
  it("leads with the verdict and shows the flow with folded files, a failed card and a sub-agent", async () => {
    renderAt("/app/admin/trajectories/users/user_a/turns/session_1/turn_1?back=status%3Dfailed")
    const verdict = await screen.findByTestId("lite-verdict")
    // The AI's closing explanation is the verdict; the template only steps in when the AI said nothing.
    expect(verdict.textContent).toBe("视频已生成，但发布时发现抖音登录已过期")
    expect(screen.getByText("3.2")).toBeTruthy()
    const cards = screen.getAllByTestId("lite-step")
    expect(cards.map((card) => card.getAttribute("data-step-type"))).toEqual([
      "user_input",
      "assistant",
      "tool",
      "tool",
      "agent",
      "assistant",
    ])
    expect(screen.getByTestId("lite-step-group").textContent).toContain(
      'lite.turn.groupTitle {"label":"处理文件","count":2}',
    )
    const failed = cards.find((card) => card.getAttribute("data-status") === "failed")!
    expect(failed.textContent).toContain("发布到抖音（云电脑）")
    expect(failed.textContent).toContain("login_expired: redirected to /login")
    // The failed card opens by itself and reads its detail; the user input is open too.
    expect(await within(failed).findByText("redirected to /login")).toBeTruthy()
    expect(await screen.findByText("lite.turn.attachmentsInPro")).toBeTruthy()
    // The AI reply renders its text as markdown with the reasoning folded.
    expect((await screen.findAllByText("好的")).length).toBeGreaterThan(0)
    expect(screen.getAllByText("lite.turn.reasoning").length).toBeGreaterThan(0)
    // Hidden records never surface; the retry is an annotation.
    expect(screen.queryByText("request:1")).toBeNull()
    expect(screen.getByText(/lite.turn.retries/)).toBeTruthy()
    // Technical details land on the professional view at this turn's end.
    expect(screen.getByRole("link", { name: /lite.turn.technicalDetails/ }).getAttribute("href")).toBe(
      "/app/admin/trajectories/sessions/session_1?at=40",
    )
    expect(screen.getByRole("link", { name: "lite.turn.backToUser" }).getAttribute("href")).toBe(
      "/app/admin/trajectories/users/user_a?status=failed",
    )
    const raw = within(failed).getByRole("link", { name: /lite.turn.rawRecord/ })
    expect(raw.getAttribute("href")).toBe(
      "/app/admin/trajectories/sessions/session_1?at=40&record=tool%3Apub",
    )
    // The sub-agent folds its steps until asked.
    const agent = cards.find((card) => card.getAttribute("data-step-type") === "agent")!
    expect(agent.textContent).toContain("排查登录态")
    expect(within(agent).queryByText("云电脑登录")).toBeNull()
    fireEvent.click(within(agent).getByRole("button", { name: /lite.turn.showAgentSteps/ }))
    expect(within(agent).getByText("云电脑登录")).toBeTruthy()
  })
})

describe("生成提示词", () => {
  it("shows every prompt of the turn in full above the flow, sub-tasks included", async () => {
    renderAt("/app/admin/trajectories/users/user_a/turns/session_1/turn_1")
    const section = await screen.findByTestId("lite-media-prompts")
    const card = within(section).getByTestId("lite-media-prompt")
    expect(within(card).getByTestId("lite-media-prompt-text").textContent).toBe(FULL_PROMPT)
    expect(card.textContent).toContain('lite.prompts.shot {"n":2}')
    expect(card.textContent).toContain("lite.prompts.bySub")
    expect(card.textContent).toContain("1080p")
    expect(card.textContent).toContain('lite.prompts.seconds {"count":8}')
    // No model in the call: the composer's choice applied.
    expect(card.textContent).toContain("lite.prompts.composerChoice")
    expect(card.textContent).toContain("lite.prompts.role.first_frame")
    expect(within(card).getByRole("button", { name: "lite.prompts.copy" })).toBeTruthy()
    expect(within(card).getByRole("link", { name: "lite.turn.rawRecord" }).getAttribute("href")).toBe(
      "/app/admin/trajectories/sessions/session_1?at=40&record=tool%3Agen",
    )
    expect(screen.getByRole("link", { name: /lite.replay.open/ }).getAttribute("href")).toBe(
      "/app/admin/trajectories/users/user_a/sessions/session_1?turn=turn_1",
    )
  })
})

describe("会话回放", () => {
  it("reads the session as a conversation and loads later turns below", async () => {
    renderAt("/app/admin/trajectories/users/user_a/sessions/session_1?back=status%3Dfailed")
    await screen.findByTestId("trajectory-lite-replay")
    expect(screen.getByText("国庆活动预热视频")).toBeTruthy()
    expect(screen.getAllByTestId("replay-turn")).toHaveLength(1)
    expect(screen.getByTestId("replay-user").textContent).toContain("把刚才那条火锅视频发到抖音")
    expect((await screen.findAllByText("好的")).length).toBeGreaterThan(0)
    expect(screen.getByTestId("lite-media-prompt-text").textContent).toBe(FULL_PROMPT)
    expect(screen.getByText("用哪个方案？")).toBeTruthy()
    expect(screen.getByText("方案 A")).toBeTruthy()
    expect(screen.getByTestId("replay-reply-failed").textContent).toContain("lite.verdict.failedReply")
    expect(screen.getByTestId("replay-turn-line").getAttribute("href")).toBe(
      "/app/admin/trajectories/users/user_a/turns/session_1/turn_1?back=status%3Dfailed",
    )
    expect(screen.getByRole("link", { name: "lite.turn.backToUser" }).getAttribute("href")).toBe(
      "/app/admin/trajectories/users/user_a?status=failed",
    )
    fireEvent.click(screen.getByRole("button", { name: /lite.replay.more/ }))
    await waitFor(() => expect(screen.getAllByTestId("replay-turn")).toHaveLength(2))
    expect(screen.getByText("再来一条")).toBeTruthy()
    expect(screen.getByText("lite.replay.end")).toBeTruthy()
    expect(get.mock.calls.map(([path]) => path).filter((path) => path.includes("/replay"))).toEqual([
      "/api/admin/trajectories/sessions/session_1/replay?limit=10",
      "/api/admin/trajectories/sessions/session_1/replay?cursor=c2&limit=10",
    ])
  })

  it("reads ahead to the turn named in the URL and scrolls to it once", async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    renderAt("/app/admin/trajectories/users/user_a/sessions/session_1?turn=turn_2")
    await waitFor(() => expect(screen.getAllByTestId("replay-turn")).toHaveLength(2))
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1))
    expect((scroll.mock.contexts[0] as HTMLElement).id).toBe("turn-turn_2")
  })
})

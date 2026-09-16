// The idle read budget of an open session page (plan 4.3). With the watermark
// socket open, its hints bring commits in at once and the page's safety polls
// stay within 4 requests a minute: at most 20 in 5 idle minutes.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import type { AuthUser } from "@/shared/types/api"
import type { TrajectoryWatermark } from "@/shared/ws/events"
import { trajectoryApi } from "../api/endpoints"
import { purgeSyncs } from "../api/registry"
import { useTrajectoryAccess } from "../stores/access"
import { useTrajectoryView } from "../stores/view"
import type { ProjectionState, SessionHeader, TrajectoryEvent } from "../types/protocol"
import { addSeq, lteSeq, ltSeq } from "../utils/seq"
import { TrajectorySessionPage } from "./TrajectorySessionPage"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

vi.mock("../api/endpoints", () => ({
  TRAJECTORY_API: "/api/admin/trajectories",
  trajectoryApi: {
    listSessions: vi.fn(),
    header: vi.fn(),
    records: vi.fn(),
    record: vi.fn(),
    recordRefs: vi.fn(),
    events: vi.fn(),
    checkpoint: vi.fn(),
    search: vi.fn(),
    payload: vi.fn(),
    payloadMeta: vi.fn(),
    blob: vi.fn(),
    createExport: vi.fn(),
    exportStatus: vi.fn(),
    downloadExport: vi.fn(),
  },
}))

/** A healthy watermark socket: open throughout, its frames delivered by the test. */
const socket = vi.hoisted(() => {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  return {
    connected: true,
    handlers,
    on(event: string, handler: (data: unknown) => void) {
      const set = handlers.get(event) ?? new Set()
      set.add(handler)
      handlers.set(event, set)
      return () => {
        set.delete(handler)
      }
    },
    emit(event: string, data: unknown) {
      for (const handler of handlers.get(event) ?? []) handler(data)
    },
    connect: async () => undefined,
    disconnect: () => undefined,
  }
})

vi.mock("../api/socket", () => ({ trajectorySocket: socket, sendTrajectoryMessage: () => true }))

interface GoldenFixture {
  events: TrajectoryEvent[]
  expected_state: ProjectionState
}

const fixtures = import.meta.glob<GoldenFixture>("../../../../../backend/trajectory/fixtures/*.json", {
  eager: true,
  import: "default",
})
const EVENTS = Object.entries(fixtures).find(([path]) => path.endsWith("/session_v1.json"))![1].events
const HEAD = EVENTS[EVENTS.length - 1].seq
const api = vi.mocked(trajectoryApi)

const HEADER: SessionHeader = {
  session_id: "session_a",
  user_id: "owner_a",
  trajectory_id: "trj_a",
  title: "fixture",
  owner: { user_id: "owner_a", username: "user_a", email: null },
  workspace: { id: "ws_a", name: "Workspace A" },
  workspace_id: "ws_a",
  running_status: "idle",
  recording_status: "recording",
  coverage_start: EVENTS[0].occurred_at,
  last_activity_at: EVENTS[EVENTS.length - 1].occurred_at,
  model: "fixture",
  agent: "build",
  committed_seq: HEAD,
  projected_through_seq: HEAD,
  through_seq: HEAD,
  statistics: {
    request_count: 3,
    tool_count: 1,
    error_count: 1,
    unknown_count: 0,
    input_tokens: 45,
    output_tokens: 10,
    usage_complete: false,
    duration_ms: null,
    through_seq: HEAD,
    coverage_start: null,
  },
  agents: [],
  capabilities: { recording: true, admin_read: true, export: true, refs: true },
  projector_version: 1,
}

const HINT: TrajectoryWatermark = {
  user_id: "owner_a",
  owner_user_id: "owner_a",
  session_id: "session_a",
  trajectory_id: "trj_a",
  committed_seq: HEAD,
}

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
const effectiveSeq = () => screen.queryByTestId("trajectory-effective-seq")?.getAttribute("data-seq")

/** Requests made so far, per endpoint. */
const counts = (): Record<string, number> =>
  Object.fromEntries(Object.entries(api).map(([name, call]) => [name, vi.mocked(call).mock.calls.length]))

/** Requests per endpoint since `before`; endpoints that were not called are left out. */
function since(before: Record<string, number>): Record<string, number> {
  return Object.fromEntries(
    Object.entries(counts())
      .map(([name, count]) => [name, count - before[name]] as const)
      .filter(([, count]) => count > 0),
  )
}

/** The live page, caught up to the head, with its subscription answered. */
async function openLive() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/app/admin/trajectories/sessions/session_a"]}>
        <TrajectorySessionPage sessionId="session_a" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  for (let step = 0; step < 100 && effectiveSeq() !== HEAD; step += 1) await advance(10)
  expect(effectiveSeq()).toBe(HEAD)
  act(() => socket.emit("subscribed", HINT))
  await advance(10)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  socket.connected = true
  socket.handlers.clear()
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  useTrajectoryView.getState().reset()
  useAuthStore.setState({
    user: { id: "admin_viewer", role: "admin" } as unknown as AuthUser,
    isAuthenticated: true,
    isLoading: false,
  })
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    bottom: 600,
    right: 960,
    width: 960,
    height: 600,
    toJSON: () => ({}),
  } as DOMRect)
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(600)
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(960)
  api.header.mockResolvedValue(HEADER)
  api.checkpoint.mockResolvedValue({ checkpoint: null, through_seq: HEAD })
  api.events.mockImplementation(async (_sid, params) => {
    const page = EVENTS.filter(
      (item) => ltSeq(params.afterSeq, item.seq) && (!params.untilSeq || lteSeq(item.seq, params.untilSeq)),
    )
    const last = page[page.length - 1]?.seq ?? params.afterSeq
    return {
      events: page,
      from_seq: page[0]?.seq ?? params.afterSeq,
      through_seq: last,
      until_seq: params.untilSeq ?? HEAD,
      has_more: false,
      committed_seq: HEAD,
    }
  })
  api.records.mockResolvedValue({
    items: [],
    next_cursor: null,
    has_more: false,
    through_seq: HEAD,
    projector_version: 1,
  } as never)
})

afterEach(() => {
  cleanup()
  act(() => purgeSyncs())
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe("idle session page with the watermark socket open", () => {
  it("makes at most 20 requests in 5 minutes: events every 30 s and the header every 60 s", async () => {
    await openLive()
    const before = counts()
    await advance(5 * 60_000)
    const idle = since(before)
    expect(Object.values(idle).reduce((sum, count) => sum + count, 0)).toBeLessThanOrEqual(20)
    expect(idle).toEqual({ events: 10, header: 5 })
    expect(effectiveSeq()).toBe(HEAD)
  })

  it("reads a hinted commit and the header at once instead of waiting for a poll", async () => {
    await openLive()
    await advance(10_000)
    const before = counts()
    act(() => socket.emit("trajectory.available", { ...HINT, committed_seq: addSeq(HEAD, 1) }))
    await advance(10)
    expect(since(before)).toEqual({ events: 1, header: 1 })
  })
})

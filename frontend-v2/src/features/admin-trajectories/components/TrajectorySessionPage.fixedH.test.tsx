import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import type { AuthUser } from "@/shared/types/api"
import { trajectoryApi } from "../api/endpoints"
import { purgeSyncs } from "../api/registry"
import { useTrajectoryAccess } from "../stores/access"
import { useTrajectoryView } from "../stores/view"
import type { CheckpointResponse, ProjectionState, SessionHeader, TrajectoryEvent } from "../types/protocol"
import { replay } from "../utils/projector"
import { lteSeq, ltSeq } from "../utils/seq"
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
    events: vi.fn(),
    checkpoint: vi.fn(),
    search: vi.fn(),
    payload: vi.fn(),
    createExport: vi.fn(),
    exportStatus: vi.fn(),
    downloadExport: vi.fn(),
  },
}))

vi.mock("../api/socket", () => ({
  trajectorySocket: {
    on: () => () => undefined,
    connect: async () => undefined,
    disconnect: () => undefined,
    send: () => true,
  },
  sendTrajectoryMessage: () => true,
}))

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
const HEAD_STATE = replay(EVENTS)
const api = vi.mocked(trajectoryApi)
/** Records that only exist after seq 14: none may appear while position 14 is loading. */
const FUTURE_IDS = Object.values(HEAD_STATE.records)
  .filter((record) => !lteSeq(record.start_seq, "14"))
  .map((record) => record.record_id)

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
  capabilities: { recording: true, admin_read: true, export: true },
  projector_version: 1,
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => (resolve = done))
  return { promise, resolve }
}

function renderAt(search: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/app/admin/trajectories/sessions/session_a?${search}`]}>
        <TrajectorySessionPage sessionId="session_a" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function assertNoFuture() {
  expect(screen.queryByTestId("trajectory-summary-preview")).toBeNull()
  const shown = screen
    .queryAllByTestId("trajectory-record-row")
    .map((row) => row.getAttribute("data-record-id"))
  expect(shown.filter((id) => id && FUTURE_IDS.includes(id))).toEqual([])
}

beforeEach(() => {
  vi.clearAllMocks()
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
    items: Object.values(HEAD_STATE.records).map((record) => ({
      ...record,
      data: undefined,
      blocks: undefined,
    })),
    next_cursor: null,
    has_more: false,
    through_seq: HEAD,
    projector_version: 1,
  } as never)
})

afterEach(() => {
  cleanup()
  act(() => purgeSyncs())
  vi.restoreAllMocks()
})

describe("fixed replay position while older history loads", () => {
  it("never shows head summaries or future rows before position 14 is ready", async () => {
    const older = deferred<CheckpointResponse>()
    api.checkpoint.mockImplementation((_sid, atSeq) =>
      atSeq === undefined
        ? Promise.resolve({
            checkpoint: { through_seq: HEAD, projector_version: 1, state: HEAD_STATE, digest: null },
            through_seq: HEAD,
          })
        : older.promise,
    )
    renderAt("at=14")
    const placeholder = await screen.findByTestId("trajectory-position-loading")
    expect(placeholder.getAttribute("data-seq")).toBe("14")
    await waitFor(() => expect(api.checkpoint).toHaveBeenCalledWith("session_a", "14", expect.anything()))
    assertNoFuture()
    expect(api.records).not.toHaveBeenCalled()

    await act(async () => older.resolve({ checkpoint: null, through_seq: "14" }))
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe("14"),
    )
    expect(screen.queryByTestId("trajectory-position-loading")).toBeNull()
    expect(screen.getAllByTestId("trajectory-record-row").length).toBeGreaterThan(0)
    assertNoFuture()
    expect(api.records).not.toHaveBeenCalled()
  })

  it("keeps the fast summary first paint when following live", async () => {
    api.checkpoint.mockImplementation(() => deferred<CheckpointResponse>().promise)
    renderAt("")
    expect(await screen.findByTestId("trajectory-summary-preview")).toBeTruthy()
    expect(screen.queryByTestId("trajectory-position-loading")).toBeNull()
    await waitFor(() =>
      expect(api.records).toHaveBeenCalledWith(
        "session_a",
        expect.objectContaining({ throughSeq: HEAD }),
        expect.anything(),
      ),
    )
  })
})

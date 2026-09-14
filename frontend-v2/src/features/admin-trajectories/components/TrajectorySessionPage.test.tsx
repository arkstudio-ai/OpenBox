import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { trajectoryApi } from "../api/endpoints"
import { purgeSyncs } from "../api/registry"
import { useTrajectoryAccess } from "../stores/access"
import { useTrajectoryView } from "../stores/view"
import type { ProjectionState, SessionHeader, TrajectoryEvent } from "../types/protocol"
import { eventsForRecord, replay } from "../utils/projector"
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

const sent: unknown[] = []
vi.mock("../api/socket", () => ({
  trajectorySocket: {
    on: () => () => undefined,
    connect: async () => undefined,
    disconnect: () => undefined,
    send: () => true,
  },
  sendTrajectoryMessage: (message: unknown) => {
    sent.push(message)
    return true
  },
}))

interface GoldenFixture {
  events: TrajectoryEvent[]
  expected_state: ProjectionState
}

const fixtures = import.meta.glob<GoldenFixture>("../../../../../backend/trajectory/fixtures/*.json", {
  eager: true,
  import: "default",
})
const golden = Object.entries(fixtures).find(([path]) => path.endsWith("/session_v1.json"))![1]
const EVENTS = golden.events
const HEAD = EVENTS[EVENTS.length - 1].seq
const api = vi.mocked(trajectoryApi)

const HEADER: SessionHeader = {
  session_id: "session_a",
  user_id: "owner_a",
  trajectory_id: "trj_a",
  title: "旧会话续聊与工具回放",
  owner: { user_id: "owner_a", username: "user_a", email: "a@example.test" },
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
  // Deliberately different from any position: the page must never show these.
  statistics: {
    request_count: 999,
    tool_count: 999,
    error_count: 0,
    unknown_count: 0,
    input_tokens: 1,
    output_tokens: 1,
    usage_complete: true,
    duration_ms: 1,
    through_seq: HEAD,
    coverage_start: null,
  },
  agents: [],
  capabilities: { recording: true, admin_read: true, export: true },
  projector_version: 1,
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

beforeEach(() => {
  vi.clearAllMocks()
  sent.length = 0
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
      (event) =>
        ltSeq(params.afterSeq, event.seq) && (!params.untilSeq || lteSeq(event.seq, params.untilSeq)),
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
  api.record.mockImplementation(async (_sid, recordId, throughSeq) => {
    const upTo = EVENTS.filter((event) => lteSeq(event.seq, throughSeq))
    const record = replay(upTo).records[recordId]
    if (!record) throw new ApiError(404, "HTTP_404", "not at this position")
    return {
      record: { ...record, as_of_seq: throughSeq, events: eventsForRecord(record, upTo) },
      through_seq: throughSeq,
      projector_version: 1,
    }
  })
})

afterEach(() => {
  cleanup()
  act(() => purgeSyncs())
  vi.restoreAllMocks()
})

describe("TrajectorySessionPage", () => {
  it("replays a historical position without leaking later results into rows, totals or details", async () => {
    renderAt("at=17&record=tool%3Acall_a")
    const header = await screen.findByTestId("trajectory-header")
    expect(within(header).getByText("user_a")).toBeTruthy()
    expect(within(header).getByText("Workspace A")).toBeTruthy()

    await waitFor(() =>
      expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe("17"),
    )
    expect(screen.getByTestId("trajectory-live-head").getAttribute("data-seq")).toBe(HEAD)
    expect(screen.getByTestId("trajectory-stat-requests").querySelector("dd")?.textContent).toBe("1")
    expect(screen.getByTestId("trajectory-stat-tools").querySelector("dd")?.textContent).toBe("1")

    const rowIds = screen
      .getAllByTestId("trajectory-record-row")
      .map((row) => row.getAttribute("data-record-id"))
    expect(rowIds).toContain("tool:call_a")
    expect(rowIds).not.toContain("question:q_a")
    expect(new Set(rowIds).size).toBe(rowIds.length)

    const inspector = await screen.findByTestId("trajectory-inspector")
    expect(inspector.getAttribute("data-record-id")).toBe("tool:call_a")
    fireEvent.click(await screen.findByTestId("trajectory-tab-result"))
    const result = await screen.findByTestId("trajectory-tool-result")
    expect(result.textContent).toContain("one")
    expect(result.textContent).not.toContain("one two")
    expect(api.record).toHaveBeenCalledWith("session_a", "tool:call_a", "17", expect.anything())
    expect(api.record.mock.calls.every(([, , seq]) => seq === "17")).toBe(true)
  })

  it("keeps a record selected before it exists and follows the live head only on request", async () => {
    renderAt("at=14&record=question%3Aq_a")
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe("14"),
    )
    expect((await screen.findByTestId("trajectory-inspector-empty")).textContent).toContain(
      "inspector.notYet",
    )
    expect(screen.getByTestId("trajectory-newer-count").textContent).toContain(`"n":"${BigInt(HEAD) - 14n}"`)

    fireEvent.click(screen.getByTestId("trajectory-return-live"))
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe(HEAD),
    )
    expect(await screen.findByTestId("trajectory-inspector")).toBeTruthy()
    expect(screen.getByTestId("trajectory-mode").getAttribute("data-live")).toBe("true")
  })

  it("reads only: no export, write or interaction call happens while replaying", async () => {
    renderAt("at=20")
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe("20"),
    )
    fireEvent.click(screen.getByTestId("trajectory-previous-event"))
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe("19"),
    )
    fireEvent.click(screen.getByTestId("trajectory-next-step"))
    expect(api.createExport).not.toHaveBeenCalled()
    expect(api.downloadExport).not.toHaveBeenCalled()
    expect(
      sent.every((message) => ["subscribe", "unsubscribe"].includes((message as { type: string }).type)),
    ).toBe(true)
    const buttons = screen
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label") ?? button.textContent ?? "")
    expect(buttons.join(" ")).not.toMatch(/approve|answer|send message|cancel run|resume run|provision/i)
  })

  it("shows the deletion after the target is forgotten, and keeps showing it", async () => {
    renderAt("record=tool%3Acall_a")
    expect(screen.getByTestId("trajectory-session-loading")).toBeTruthy()
    await waitFor(() =>
      expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe(HEAD),
    )
    expect(await screen.findByTestId("trajectory-inspector")).toBeTruthy()

    const gone = new ApiError(410, "trajectory_content_deleted", "gone")
    for (const read of [api.header, api.checkpoint, api.events, api.record]) read.mockRejectedValue(gone)
    // The next 1 s head poll meets the 410: the engine forgets the target, which unbinds the view.
    const alert = await screen.findByTestId("trajectory-session-error", {}, { timeout: 8_000 })
    expect(alert.textContent).toBe("session.gone")
    expect(useTrajectoryView.getState().targetKey).toBeNull()
    expect(screen.queryByTestId("trajectory-session-loading")).toBeNull()
    expect(screen.queryAllByTestId("trajectory-record-row")).toHaveLength(0)
    expect(screen.queryByTestId("trajectory-inspector")).toBeNull()
    expect(sent.at(-1)).toEqual({ type: "unsubscribe", session_id: "session_a" })

    // Stable: the forgotten target is never rebound, so neither loading nor its content returns.
    await new Promise((resolve) => setTimeout(resolve, 1_500))
    expect(screen.getByTestId("trajectory-session-error").textContent).toBe("session.gone")
    expect(useTrajectoryView.getState().targetKey).toBeNull()
    expect(screen.queryByTestId("trajectory-session")).toBeNull()
  }, 15_000)

  it("says recording has not started for a session without a trajectory and opens no stream", async () => {
    api.header.mockResolvedValue({
      ...HEADER,
      trajectory_id: null,
      committed_seq: "0",
      projected_through_seq: "0",
      through_seq: "0",
    })
    renderAt("")
    expect(await screen.findByTestId("trajectory-not-recorded")).toBeTruthy()
    expect(api.checkpoint).not.toHaveBeenCalled()
    expect(api.events).not.toHaveBeenCalled()
  })
})

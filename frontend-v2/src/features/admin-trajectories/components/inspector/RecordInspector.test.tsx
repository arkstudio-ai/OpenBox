// The inspector asks for record details with references only where the
// server offers them, and follows a moving position at most every 2 s.
import { QueryClient } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { trajectoryApi } from "../../api/endpoints"
import { useTrajectoryAccess } from "../../stores/access"
import type { TrajectoryRecord } from "../../types/protocol"
import { inspectorEnv, makeRecord, withInspector } from "../testing/harness"
import { RecordInspector } from "./RecordInspector"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})
vi.mock("../../api/endpoints", () => ({
  trajectoryApi: { record: vi.fn(), recordRefs: vi.fn(), blob: vi.fn(), payload: vi.fn() },
}))

const api = vi.mocked(trajectoryApi)
const SHA = "5e".repeat(32)
const REQUEST = makeRecord({
  record_id: "request:req_1",
  kind: "request",
  request_id: "req_1",
  as_of_seq: "10",
  data: { capture_level: "adapter_input", input: { system: "Be precise" } },
})
const detail = (record: TrajectoryRecord) => ({ record, through_seq: record.as_of_seq, projector_version: 1 })

let client: QueryClient

function inspect(throughSeq: string, options: { refs?: boolean; settle?: boolean } = {}) {
  return withInspector(
    <RecordInspector selectedId="request:req_1" settle={options.settle ?? false} />,
    inspectorEnv([REQUEST], { throughSeq, refs: options.refs ?? false, live: !!options.settle }),
    client,
  )
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
})

afterEach(() => {
  cleanup()
  client.clear()
  vi.useRealTimers()
  vi.resetAllMocks()
})

describe("record details", () => {
  it("are read expanded unless the header offers references", async () => {
    // Expanded details hold no references: captured data shaped like one is shown, never read.
    const lookalike = { $ref: { sha256: SHA } }
    api.record.mockResolvedValue(
      detail({ ...REQUEST, data: { ...REQUEST.data, input: { system: lookalike } } }),
    )
    render(inspect("10"))
    await waitFor(() => expect(screen.getByTestId("trajectory-summary")).toBeTruthy())
    expect(api.record).toHaveBeenCalledWith("ses_test", "request:req_1", "10", expect.any(AbortSignal))
    expect(api.recordRefs).not.toHaveBeenCalled()
    expect(api.blob).not.toHaveBeenCalled()
    expect(screen.queryByTestId("trajectory-content-loading")).toBeNull()
  })

  it("keep references where the header offers them, and the panel shown reads them", async () => {
    const stored = { $ref: { sha256: SHA, size_bytes: 10, media_type: "application/json", kind: "system" } }
    api.recordRefs.mockResolvedValue(
      detail({ ...REQUEST, data: { ...REQUEST.data, input: { system: stored } } }),
    )
    api.blob.mockResolvedValue("Be precise")
    render(inspect("10", { refs: true }))
    await waitFor(() => expect(screen.getByTestId("trajectory-summary")).toBeTruthy())
    expect(api.recordRefs).toHaveBeenCalledWith("ses_test", "request:req_1", "10", expect.any(AbortSignal))
    expect(api.record).not.toHaveBeenCalled()
    expect(api.blob).toHaveBeenCalledWith("ses_test", SHA, "10", expect.any(AbortSignal))
    expect(screen.getByTestId("trajectory-summary").textContent).not.toContain(SHA)
  })

  it("follow a moving position at most every 2 s", async () => {
    vi.useFakeTimers()
    api.record.mockImplementation(async (_sid, _id, seq) => detail({ ...REQUEST, as_of_seq: seq }))
    const view = render(inspect("10", { settle: true }))
    await act(() => vi.advanceTimersByTimeAsync(0))
    const inspector = () => screen.getByTestId("trajectory-inspector")
    expect(inspector().getAttribute("data-detail-seq")).toBe("10")

    view.rerender(inspect("11", { settle: true }))
    view.rerender(inspect("12", { settle: true }))
    await act(() => vi.advanceTimersByTimeAsync(1_999))
    expect(inspector().getAttribute("data-detail-seq")).toBe("10")
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(inspector().getAttribute("data-detail-seq")).toBe("12")
    expect(api.record.mock.calls.map(([, , seq]) => seq)).toEqual(["10", "12"])
  })
})

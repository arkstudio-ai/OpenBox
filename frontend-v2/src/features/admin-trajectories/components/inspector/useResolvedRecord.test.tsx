// Reading `$ref` values for the inspector: at the shown watermark, once per
// digest, following nested references, with visible loading, unavailable and
// failed states, and without ever bridging a refresh with a later position.
import { QueryClient } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/shared/api/http"
import { trajectoryApi } from "../../api/endpoints"
import { useTrajectoryAccess } from "../../stores/access"
import { inspectorEnv, makeRecord, withInspector } from "../testing/harness"
import type { InspectedRecord } from "./types"
import { useResolvedRecord, useResolvedValue, type Resolution } from "./useResolvedRecord"

vi.mock("../../api/endpoints", () => ({ trajectoryApi: { blob: vi.fn() } }))

const blob = vi.mocked(trajectoryApi.blob)
let client: QueryClient

const sha = (n: number) => n.toString(16).padStart(64, "0")
const ref = (digest: string) => ({
  $ref: {
    sha256: digest,
    size_bytes: 4096,
    media_type: "application/json",
    kind: "message",
    payload_id: "pld_1",
  },
})
const valueOf = <T,>(resolution: Resolution<T>) =>
  resolution.status === "ready" ? resolution.value : undefined

function deferred() {
  let resolve!: (value: unknown) => void
  const promise = new Promise<unknown>((done) => (resolve = done))
  return { promise, resolve }
}

function at(throughSeq = "10") {
  const env = inspectorEnv([], { throughSeq, refs: true })
  return function Wrapper({ children }: { children: ReactNode }) {
    return <>{withInspector(children, env, client)}</>
  }
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
})

afterEach(() => {
  cleanup()
  client.clear()
  vi.resetAllMocks()
})

describe("without capabilities.refs", () => {
  it("hands every record back untouched and reads nothing, even one holding a look-alike", () => {
    const env = inspectorEnv([], { throughSeq: "10", refs: false })
    const wrapper = ({ children }: { children: ReactNode }) => <>{withInspector(children, env, client)}</>
    const record = makeRecord({
      record_id: "request:req_1",
      kind: "request",
      data: { input: { system: ref(sha(11)), messages: [ref(sha(12))] } },
    })
    const whole = renderHook(() => useResolvedRecord(record), { wrapper })
    expect(whole.result.current.resolution).toEqual({ status: "ready", value: record })
    expect(valueOf(whole.result.current.resolution)).toBe(record)
    const shown = renderHook(() => useResolvedRecord(record, ["input"]), { wrapper })
    expect(valueOf(shown.result.current.resolution)).toBe(record)
    expect(blob).not.toHaveBeenCalled()
    expect(client.getQueryCache().getAll()).toHaveLength(0)
  })
})

describe("resolving references", () => {
  it("hands a value without references back at once, as the same object, reading nothing", () => {
    const value = { input: { messages: [{ role: "user", content: "hi" }] } }
    const { result } = renderHook(() => useResolvedValue(value), { wrapper: at() })
    expect(result.current.resolution.status).toBe("ready")
    expect(valueOf(result.current.resolution)).toBe(value)
    expect(blob).not.toHaveBeenCalled()
  })

  it("reads each digest once at the shown watermark and follows references inside read content", async () => {
    const content: Record<string, unknown> = {
      [sha(1)]: { role: "system", content: [ref(sha(2))] },
      [sha(2)]: { type: "text", text: "Be precise" },
    }
    blob.mockImplementation(async (_sid, digest) => content[digest])
    const value = { messages: [ref(sha(1)), ref(sha(1))] }
    const { result } = renderHook(() => useResolvedValue(value), { wrapper: at("37") })
    expect(result.current.resolution.status).toBe("loading")
    await waitFor(() => expect(result.current.resolution.status).toBe("ready"))
    const system = { role: "system", content: [{ type: "text", text: "Be precise" }] }
    expect(valueOf(result.current.resolution)).toEqual({ messages: [system, system] })
    expect(blob.mock.calls.map(([sid, digest, seq]) => [sid, digest, seq])).toEqual([
      ["ses_test", sha(1), "37"],
      ["ses_test", sha(2), "37"],
    ])

    // Another panel, record or position needing the same content reads nothing more.
    const again = renderHook(() => useResolvedValue([ref(sha(2))]), { wrapper: at("40") })
    expect(valueOf(again.result.current.resolution)).toEqual([{ type: "text", text: "Be precise" }])
    expect(blob).toHaveBeenCalledTimes(2)
  })

  it("says when content is deleted or not visible yet instead of loading forever", async () => {
    blob.mockImplementation(async (_sid, digest) => {
      throw digest === sha(3)
        ? new ApiError(410, "trajectory_content_deleted", "deleted")
        : new ApiError(404, "HTTP_404", "not yet")
    })
    const deleted = renderHook(() => useResolvedValue([ref(sha(3))]), { wrapper: at() })
    await waitFor(() =>
      expect(deleted.result.current.resolution).toEqual({ status: "unavailable", availability: "deleted" }),
    )
    const pending = renderHook(() => useResolvedValue([ref(sha(4))]), { wrapper: at() })
    await waitFor(() =>
      expect(pending.result.current.resolution).toEqual({ status: "unavailable", availability: "pending" }),
    )
  })

  it("fails visibly and reads again on retry", async () => {
    blob.mockRejectedValue(new ApiError(500, "HTTP_500", "unavailable"))
    const { result } = renderHook(() => useResolvedValue([ref(sha(5))]), { wrapper: at() })
    await waitFor(() => expect(result.current.resolution.status).toBe("error"))
    blob.mockResolvedValue("recovered")
    act(() => result.current.retry())
    await waitFor(() => expect(valueOf(result.current.resolution)).toEqual(["recovered"]))
  })
})

describe("resolving a record", () => {
  it("reads only the fields a panel shows and hands an untouched record back as is", async () => {
    blob.mockResolvedValue("You are careful")
    const record = makeRecord({
      record_id: "request:req_1",
      kind: "request",
      data: { input: { system: ref(sha(6)) }, output: ref(sha(7)) },
    })
    const { result } = renderHook(() => useResolvedRecord(record, ["input"]), { wrapper: at() })
    await waitFor(() => expect(result.current.resolution.status).toBe("ready"))
    expect(valueOf(result.current.resolution)?.data).toEqual({
      input: { system: "You are careful" },
      output: ref(sha(7)),
    })
    expect(blob.mock.calls.map(([, digest]) => digest)).toEqual([sha(6)])

    const plain = makeRecord({ record_id: "tool:call_a", kind: "tool", data: { output: "ok" } })
    const untouched = renderHook(() => useResolvedRecord(plain), { wrapper: at() })
    expect(valueOf(untouched.result.current.resolution)).toBe(plain)
  })

  it("bridges a newer detail of the same record with the earlier one, never with a later one", async () => {
    blob.mockResolvedValueOnce("first output")
    const tool = (asOf: string, digest: string): InspectedRecord =>
      makeRecord({ record_id: "tool:call_a", kind: "tool", as_of_seq: asOf, data: { output: ref(digest) } })
    const { result, rerender } = renderHook(({ record }) => useResolvedRecord(record), {
      wrapper: at("20"),
      initialProps: { record: tool("10", sha(8)) },
    })
    await waitFor(() => expect(valueOf(result.current.resolution)?.data.output).toBe("first output"))
    const earlier = valueOf(result.current.resolution)

    const newer = deferred()
    blob.mockImplementationOnce(() => newer.promise)
    rerender({ record: tool("12", sha(9)) })
    expect(result.current.resolution).toEqual({ status: "ready", value: earlier })
    await act(async () => newer.resolve("second output"))
    await waitFor(() => expect(valueOf(result.current.resolution)?.data.output).toBe("second output"))

    const older = deferred()
    blob.mockImplementationOnce(() => older.promise)
    rerender({ record: tool("8", sha(10)) })
    expect(result.current.resolution.status).toBe("loading")
    await act(async () => older.resolve("old output"))
    await waitFor(() => expect(valueOf(result.current.resolution)?.data.output).toBe("old output"))
  })
})

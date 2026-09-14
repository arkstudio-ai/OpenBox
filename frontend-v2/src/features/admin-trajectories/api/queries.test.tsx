import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { useTrajectoryAccess } from "../stores/access"
import type { PayloadMeta, RecordDetail, SessionHeader } from "../types/protocol"
import { EMPTY_LIST_PARAMS } from "../utils/params"
import { saveBlob } from "../utils/download"
import { purgeTrajectoryAccess, StaleAccessError } from "./access"
import { trajectoryApi } from "./endpoints"
import { trajectoryKeys } from "./keys"
import {
  useCreateExport,
  useExportDownload,
  useHeaderHintRefresh,
  usePayload,
  useRecordDetail,
  useRecordSearch,
  useSessionHeader,
  useSessionList,
  useSessionListProbe,
} from "./queries"

vi.mock("./endpoints", () => ({
  trajectoryApi: {
    listSessions: vi.fn(),
    header: vi.fn(),
    record: vi.fn(),
    recordRefs: vi.fn(),
    search: vi.fn(),
    payload: vi.fn(),
    payloadMeta: vi.fn(),
    createExport: vi.fn(),
    downloadExport: vi.fn(),
    events: vi.fn(),
    checkpoint: vi.fn(),
  },
}))
vi.mock("../utils/download", () => ({ saveBlob: vi.fn() }))

const api = vi.mocked(trajectoryApi)
let client: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => (resolve = done))
  return { promise, resolve }
}

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
const HEADER = { session_id: "ses_b" } as unknown as SessionHeader

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  useAuthStore.setState({
    user: { id: "admin-a", role: "admin" } as unknown as AuthUser,
    isAuthenticated: true,
    isLoading: false,
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  client.clear()
  vi.clearAllMocks()
})

describe("fixed-watermark search", () => {
  it("never shows hits from another watermark while the new one loads", async () => {
    api.search.mockImplementation(async (_sid, params) => ({
      items: [
        {
          record_id: `tool:at-${params.throughSeq}`,
          seq: params.throughSeq,
          kind: "tool",
          preview: "later output",
        },
      ],
      next_cursor: null,
      has_more: false,
      through_seq: params.throughSeq,
    }))
    const { result, rerender } = renderHook(({ at }) => useRecordSearch("ses_b", at, "output"), {
      wrapper,
      initialProps: { at: "30" },
    })
    await waitFor(() => expect(result.current.data?.pages[0].items[0].record_id).toBe("tool:at-30"))
    const pending = deferred<never>()
    api.search.mockImplementationOnce(() => pending.promise)
    rerender({ at: "12" })
    expect(result.current.data).toBeUndefined()
  })
})

describe("protected content", () => {
  it("replaces a shown body once a revalidation finds it deleted", async () => {
    api.payload.mockResolvedValueOnce({ blob: new Blob(["secret"], { type: "text/plain" }), filename: null })
    const { result } = renderHook(() => usePayload("ses_b", "12", "pay_1"), { wrapper })
    await waitFor(() => expect(result.current.data?.availability).toBe("available"))
    api.payload.mockRejectedValue(new ApiError(410, "trajectory_content_deleted", "deleted"))
    await act(() => result.current.refetch())
    await waitFor(() =>
      expect(result.current.data).toEqual({
        availability: "deleted",
        mediaType: null,
        size: null,
        blob: null,
        text: null,
      }),
    )
    expect(result.current.error).toBeNull()
  })

  it("says content was not produced yet at this position", async () => {
    api.payload.mockRejectedValueOnce(new ApiError(404, "HTTP_404", "not yet"))
    const { result } = renderHook(() => usePayload("ses_b", "3", "pay_1"), { wrapper })
    await waitFor(() => expect(result.current.data?.availability).toBe("pending"))
  })
})

describe("shown content revalidation", () => {
  // A replaced body reaches the hook through TanStack Query's zero-delay notification timer; the
  // fake clock runs a zero-delay timer queued while it is already running timers 1 ms later.
  const settle = () => advance(1)
  const png = () => ({ blob: new Blob(["png"], { type: "image/png" }), filename: null })
  const meta = (availability: string): PayloadMeta => ({
    payload_id: "pay_1",
    availability,
    media_type: "image/png",
    size_bytes: 3,
    sha256: "ab".repeat(32),
  })

  it("reads the bytes again every 15 s where the server has no availability check", async () => {
    vi.useFakeTimers()
    api.payload.mockImplementation(async () => png())
    renderHook(() => usePayload("ses_b", "12", "pay_1"), { wrapper })
    await advance(0)
    expect(api.payload).toHaveBeenCalledTimes(1)
    await advance(15_000)
    expect(api.payload).toHaveBeenCalledTimes(2)
    expect(api.payloadMeta).not.toHaveBeenCalled()
  })

  it("reads the bytes once, asks ?meta=1 every 60 s and replaces the body once it is gone", async () => {
    vi.useFakeTimers()
    api.payload.mockImplementation(async () => png())
    api.payloadMeta.mockResolvedValue(meta("available"))
    const { result } = renderHook(() => usePayload("ses_b", "12", "pay_1", "meta"), { wrapper })
    await advance(0)
    expect(result.current.data?.availability).toBe("available")
    await advance(59_999)
    expect(api.payloadMeta).not.toHaveBeenCalled()
    await advance(1)
    expect(api.payloadMeta).toHaveBeenCalledWith("ses_b", "pay_1", "12", expect.any(AbortSignal))
    await settle()
    expect(result.current.data?.availability).toBe("available")

    api.payloadMeta.mockResolvedValue(meta("expired"))
    await advance(60_000)
    await settle()
    expect(api.payloadMeta).toHaveBeenCalledTimes(2)
    expect(result.current.data).toEqual({
      availability: "deleted",
      mediaType: null,
      size: null,
      blob: null,
      text: null,
    })
    // Once nothing is shown there is nothing left to check, and the bytes were never read again.
    await advance(180_000)
    expect(api.payloadMeta).toHaveBeenCalledTimes(2)
    expect(api.payload).toHaveBeenCalledTimes(1)
  })

  it("checks at once when the tab is shown again, and a 410 from the check hides the content", async () => {
    vi.useFakeTimers()
    api.payload.mockImplementation(async () => png())
    api.payloadMeta.mockRejectedValue(new ApiError(410, "trajectory_content_deleted", "deleted"))
    const { result } = renderHook(() => usePayload("ses_b", "12", "pay_1", "meta"), { wrapper })
    await advance(0)
    expect(result.current.data?.availability).toBe("available")
    act(() => void document.dispatchEvent(new Event("visibilitychange")))
    await settle()
    expect(api.payloadMeta).toHaveBeenCalledTimes(1)
    expect(result.current.data?.availability).toBe("deleted")
    expect(api.payload).toHaveBeenCalledTimes(1)
  })

  it("hides the content for an availability it does not know, even an inherited object key", async () => {
    vi.useFakeTimers()
    api.payload.mockImplementation(async () => png())
    api.payloadMeta.mockResolvedValue(meta("constructor"))
    const { result } = renderHook(() => usePayload("ses_b", "12", "pay_1", "meta"), { wrapper })
    await advance(0)
    expect(result.current.data?.availability).toBe("available")
    act(() => void document.dispatchEvent(new Event("visibilitychange")))
    await settle()
    expect(api.payloadMeta).toHaveBeenCalledTimes(1)
    expect(result.current.data?.availability).toBe("deleted")
  })
})

describe("live reads", () => {
  it("probe the session list every 30 s", async () => {
    vi.useFakeTimers()
    api.listSessions.mockResolvedValue({ items: [], next_cursor: null, has_more: false })
    renderHook(() => useSessionListProbe(EMPTY_LIST_PARAMS, true), { wrapper })
    await advance(0)
    expect(api.listSessions).toHaveBeenCalledTimes(1)
    await advance(29_999)
    expect(api.listSessions).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(api.listSessions).toHaveBeenCalledTimes(2)
  })

  it("refresh the header every 30 s without hints", async () => {
    vi.useFakeTimers()
    api.header.mockResolvedValue(HEADER)
    renderHook(() => useSessionHeader("ses_b"), { wrapper })
    await advance(0)
    expect(api.header).toHaveBeenCalledTimes(1)
    await advance(29_999)
    expect(api.header).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(api.header).toHaveBeenCalledTimes(2)
  })

  it("follow watermark hints at most every 5 s, honour the latest and read a deletion at once", async () => {
    vi.useFakeTimers()
    api.header.mockResolvedValue(HEADER)
    const { result } = renderHook(
      () => ({ header: useSessionHeader("ses_b"), hint: useHeaderHintRefresh("ses_b") }),
      { wrapper },
    )
    await advance(0)
    expect(api.header).toHaveBeenCalledTimes(1)
    act(() => result.current.hint({}))
    act(() => result.current.hint({}))
    await advance(4_999)
    expect(api.header).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(api.header).toHaveBeenCalledTimes(2)

    // Long after the last answer a hint is read at once.
    await advance(6_000)
    act(() => result.current.hint({}))
    await advance(0)
    expect(api.header).toHaveBeenCalledTimes(3)
    act(() => result.current.hint({ deleted: true }))
    await advance(0)
    expect(api.header).toHaveBeenCalledTimes(4)
  })

  it("skip hints the shown header already covers, such as the answer to a subscription", async () => {
    vi.useFakeTimers()
    api.header.mockResolvedValue({ ...HEADER, committed_seq: "40" } as SessionHeader)
    const { result } = renderHook(
      () => ({ header: useSessionHeader("ses_b"), hint: useHeaderHintRefresh("ses_b") }),
      { wrapper },
    )
    await advance(0)
    expect(api.header).toHaveBeenCalledTimes(1)
    act(() => result.current.hint({ committed_seq: "40" }))
    act(() => result.current.hint({ committed_seq: "039" }))
    await advance(10_000)
    expect(api.header).toHaveBeenCalledTimes(1)

    act(() => result.current.hint({ committed_seq: "41" }))
    await advance(0)
    expect(api.header).toHaveBeenCalledTimes(2)
  })

  it("read the header again after an answer that was already on its way when a hint arrived", async () => {
    vi.useFakeTimers()
    const first = deferred<SessionHeader>()
    api.header.mockImplementationOnce(() => first.promise)
    api.header.mockResolvedValue(HEADER)
    const { result } = renderHook(
      () => ({ header: useSessionHeader("ses_b"), hint: useHeaderHintRefresh("ses_b") }),
      { wrapper },
    )
    await advance(0)
    act(() => result.current.hint({}))
    await advance(0)
    expect(api.header).toHaveBeenCalledTimes(1)
    await act(async () => first.resolve(HEADER))
    await advance(4_999)
    expect(api.header).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(api.header).toHaveBeenCalledTimes(2)
  })
})

describe("record detail", () => {
  it("asks for references only when told to, and keeps the two answers apart", async () => {
    const answer = (data: Record<string, unknown>) =>
      ({
        record: { record_id: "request:r", data },
        through_seq: "12",
        projector_version: 1,
      }) as unknown as RecordDetail
    api.record.mockResolvedValue(answer({ input: { system: "Be precise" } }))
    api.recordRefs.mockResolvedValue(answer({ input: { system: { $ref: { sha256: "ab".repeat(32) } } } }))
    const full = renderHook(() => useRecordDetail("ses_b", "12", "request:r", { enabled: true }), { wrapper })
    await waitFor(() => expect(full.result.current.data).toBeDefined())
    expect(api.record).toHaveBeenCalledWith("ses_b", "request:r", "12", expect.any(AbortSignal))
    expect(api.recordRefs).not.toHaveBeenCalled()

    const refs = renderHook(
      () => useRecordDetail("ses_b", "12", "request:r", { enabled: true, expand: "refs" }),
      { wrapper },
    )
    await waitFor(() => expect(refs.result.current.data).toBeDefined())
    expect(api.recordRefs).toHaveBeenCalledWith("ses_b", "request:r", "12", expect.any(AbortSignal))
    expect(api.record).toHaveBeenCalledTimes(1)
    expect(full.result.current.data?.record.data).toEqual({ input: { system: "Be precise" } })
    expect(client.getQueryData(trajectoryKeys.recordRefs("admin-a#0", "ses_b", "12", "request:r"))).toEqual(
      refs.result.current.data,
    )
  })
})

describe("access latch", () => {
  it("stops a mounted list from showing or refetching data after a refusal", async () => {
    api.listSessions.mockResolvedValue({
      items: [{ session_id: "ses_b" }] as never,
      next_cursor: null,
      has_more: false,
    })
    const { result } = renderHook(() => useSessionList(EMPTY_LIST_PARAMS), { wrapper })
    await waitFor(() => expect(result.current.data?.items).toHaveLength(1))
    act(() => purgeTrajectoryAccess(client, "forbidden", 403))
    expect(result.current.data).toBeUndefined()
    expect(result.current.fetchStatus).toBe("idle")
    expect(api.listSessions).toHaveBeenCalledTimes(1)
  })
})

describe("export races", () => {
  it("does not cache a job whose creation finished after access was lost", async () => {
    const pending = deferred<{ export_id: string; status: string; through_seq: string }>()
    api.createExport.mockImplementation((_sid, _seq, signal) => {
      signal?.addEventListener("abort", () => undefined)
      return pending.promise
    })
    const { result } = renderHook(() => useCreateExport("ses_b"), { wrapper })
    let settled: Promise<unknown> | undefined
    act(() => {
      settled = result.current.mutateAsync("12").catch((error: unknown) => error)
    })
    await waitFor(() => expect(api.createExport).toHaveBeenCalled())
    expect(api.createExport.mock.calls[0][2]?.aborted).toBe(false)
    act(() => purgeTrajectoryAccess(client, "signed_out"))
    expect(api.createExport.mock.calls[0][2]?.aborted).toBe(true)
    pending.resolve({ export_id: "exp_1", status: "pending", through_seq: "12" })
    expect(await settled).toBeInstanceOf(StaleAccessError)
    expect(client.getQueryCache().findAll({ queryKey: trajectoryKeys.root })).toHaveLength(0)
  })

  it("does not file a job when access is lost between the request finishing and its success callback", async () => {
    api.createExport.mockResolvedValue({ export_id: "exp_2", status: "pending", through_seq: "12" })
    // The cache-level hook runs after mutationFn resolved and before the hook's own onSuccess.
    client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
      mutationCache: new MutationCache({ onSuccess: () => purgeTrajectoryAccess(client, "signed_out") }),
    })
    const { result } = renderHook(() => useCreateExport("ses_b"), { wrapper })
    await act(() => result.current.mutateAsync("12"))
    expect(client.getQueryCache().findAll({ queryKey: trajectoryKeys.root })).toHaveLength(0)
    expect(useTrajectoryAccess.getState().denied?.reason).toBe("signed_out")
  })

  it("does not save a download that arrives after access was lost", async () => {
    const pending = deferred<{ blob: Blob; filename: string | null }>()
    api.downloadExport.mockImplementation(() => pending.promise)
    const { result } = renderHook(() => useExportDownload("ses_b"), { wrapper })
    let settled: Promise<unknown> | undefined
    act(() => {
      settled = result.current.mutateAsync("exp_1").catch((error: unknown) => error)
    })
    await waitFor(() => expect(api.downloadExport).toHaveBeenCalled())
    act(() => useAuthStore.setState({ user: { id: "someone-else", role: "user" } as unknown as AuthUser }))
    pending.resolve({ blob: new Blob(["zip"]), filename: "exp_1.zip" })
    expect(await settled).toBeInstanceOf(StaleAccessError)
    expect(saveBlob).not.toHaveBeenCalled()
  })

  it("sends nothing when an export is started after access was latched", async () => {
    const { result } = renderHook(
      () => ({ create: useCreateExport("ses_b"), download: useExportDownload("ses_b") }),
      { wrapper },
    )
    act(() => purgeTrajectoryAccess(client, "forbidden", 403))
    await expect(result.current.create.mutateAsync("12")).rejects.toBeInstanceOf(StaleAccessError)
    await expect(result.current.download.mutateAsync("exp_1")).rejects.toBeInstanceOf(StaleAccessError)
    expect(api.createExport).not.toHaveBeenCalled()
    expect(api.downloadExport).not.toHaveBeenCalled()
  })

  it("saves a download that completes while access holds", async () => {
    api.downloadExport.mockResolvedValue({ blob: new Blob(["zip"]), filename: "exp_1.zip" })
    const { result } = renderHook(() => useExportDownload("ses_b"), { wrapper })
    await act(() => result.current.mutateAsync("exp_1"))
    expect(saveBlob).toHaveBeenCalledWith(expect.any(Blob), "exp_1.zip")
  })
})

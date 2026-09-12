import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { useTrajectoryAccess } from "../stores/access"
import { EMPTY_LIST_PARAMS } from "../utils/params"
import { saveBlob } from "../utils/download"
import { purgeTrajectoryAccess, StaleAccessError } from "./access"
import { trajectoryApi } from "./endpoints"
import { trajectoryKeys } from "./keys"
import { useCreateExport, useExportDownload, usePayload, useRecordSearch, useSessionList } from "./queries"

vi.mock("./endpoints", () => ({
  trajectoryApi: {
    listSessions: vi.fn(),
    search: vi.fn(),
    payload: vi.fn(),
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

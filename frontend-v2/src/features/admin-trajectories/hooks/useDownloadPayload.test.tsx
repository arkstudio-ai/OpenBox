import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { purgeTrajectoryAccess, StaleAccessError } from "../api/access"
import { trajectoryApi } from "../api/endpoints"
import { trajectoryKeys } from "../api/keys"
import { usePayload } from "../api/queries"
import { useTrajectoryAccess } from "../stores/access"
import { saveBlob } from "../utils/download"
import { useDownloadPayload } from "./useDownloadPayload"

vi.mock("../api/endpoints", () => ({
  trajectoryApi: { payload: vi.fn(), events: vi.fn(), checkpoint: vi.fn() },
}))
vi.mock("../utils/download", () => ({ saveBlob: vi.fn() }))

const payload = vi.mocked(trajectoryApi.payload)
let client: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const blob = (text: string) => new Blob([text], { type: "application/octet-stream" })

/**
 * Responses that only settle when told to, and optionally honour abort like
 * fetch does. Every call gets its own resolver: the shown query may re-key and
 * fetch again while a download is still travelling.
 */
function pendingResponse(honourAbort: boolean) {
  const releases: Array<() => void> = []
  const call = vi.fn(
    (_sid: string, _pid: string, _seq: string, signal?: AbortSignal) =>
      new Promise<{ blob: Blob; filename: string | null }>((resolve, reject) => {
        releases.push(() => resolve({ blob: blob("late bytes"), filename: null }))
        if (honourAbort)
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))
      }),
  )
  return { call, release: () => releases.forEach((release) => release()) }
}

/** After a purge a still-mounted, now disabled observer may leave an empty entry; none may hold content. */
function cachedTrajectoryData() {
  return client
    .getQueryCache()
    .findAll({ queryKey: trajectoryKeys.root })
    .filter((query) => query.state.data !== undefined)
}

interface Target {
  sessionId: string
  throughSeq: string
  payloadId: string
}

const SHOWN: Target = { sessionId: "ses_b", throughSeq: "37", payloadId: "pay_img" }

/**
 * The inspector's shown payload with its download control, as PayloadView
 * mounts them, after the protected content has been displayed once.
 */
async function mountWithCachedBlob() {
  const cached = blob("cached bytes")
  payload.mockResolvedValueOnce({ blob: cached, filename: null })
  const view = renderHook(
    ({ sessionId, throughSeq, payloadId }: Target) => ({
      shown: usePayload(sessionId, throughSeq, payloadId),
      download: useDownloadPayload(sessionId, throughSeq, payloadId),
    }),
    { wrapper, initialProps: SHOWN },
  )
  await waitFor(() => expect(view.result.current.shown.data?.availability).toBe("available"))
  expect(payload).toHaveBeenCalledTimes(1)
  return { ...view, cached }
}

/** Start a download without awaiting it; the returned promise resolves to the value or the error. */
function startDownload(download: { mutateAsync: () => Promise<unknown> }): Promise<unknown> {
  let settled!: Promise<unknown>
  act(() => {
    settled = download.mutateAsync().catch((error: unknown) => error)
  })
  return settled
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
  // Vitest runs without globals, so Testing Library does not unmount on its own.
  cleanup()
  client.clear()
  vi.resetAllMocks()
})

describe("useDownloadPayload", () => {
  it("fetches afresh at H on every click instead of saving the Blob on screen", async () => {
    const { result, cached } = await mountWithCachedBlob()
    const fresh = blob("fresh bytes")
    payload.mockResolvedValue({ blob: fresh, filename: null })

    await act(() => result.current.download.mutateAsync({ filename: "/workspace/out/report.bin" }))
    await act(() => result.current.download.mutateAsync())

    expect(payload).toHaveBeenCalledTimes(3)
    expect(payload.mock.calls[1].slice(0, 3)).toEqual(["ses_b", "pay_img", "37"])
    expect(payload.mock.calls[1][3]).toBeInstanceOf(AbortSignal)
    expect(saveBlob).toHaveBeenNthCalledWith(1, fresh, "report.bin")
    expect(saveBlob).toHaveBeenNthCalledWith(2, fresh, "pay_img")
    expect(vi.mocked(saveBlob).mock.calls.every(([saved]) => saved !== cached)).toBe(true)
    // The mutation result never holds the bytes.
    await waitFor(() => expect(result.current.download.data).toEqual({ saved: true }))
  })

  it.each([
    [410, "deleted"],
    [404, "pending"],
    [409, "corrupt"],
  ] as const)("saves nothing on %i and replaces the shown body with %s", async (status, availability) => {
    const { result } = await mountWithCachedBlob()
    payload.mockRejectedValueOnce(new ApiError(status, `HTTP_${status}`, "unavailable"))

    let outcome: unknown
    await act(async () => {
      outcome = await result.current.download.mutateAsync()
    })

    expect(outcome).toEqual({ saved: false, availability })
    expect(saveBlob).not.toHaveBeenCalled()
    await waitFor(() =>
      expect(result.current.shown.data).toEqual({
        availability,
        mediaType: null,
        size: null,
        blob: null,
        text: null,
      }),
    )
  })

  it("purges trajectory state and saves nothing when the server refuses the viewer", async () => {
    const { result } = await mountWithCachedBlob()
    payload.mockRejectedValueOnce(new ApiError(403, "HTTP_403", "Forbidden"))

    const outcome = await startDownload(result.current.download)

    expect(outcome).toBeInstanceOf(ApiError)
    expect(saveBlob).not.toHaveBeenCalled()
    expect(useTrajectoryAccess.getState().denied).toEqual({ reason: "forbidden", status: 403 })
    expect(cachedTrajectoryData()).toEqual([])
    await waitFor(() => expect(result.current.shown.data).toBeUndefined())
  })

  it("sends nothing once access is already latched", async () => {
    const { result } = await mountWithCachedBlob()
    act(() => purgeTrajectoryAccess(client, "forbidden", 403))

    const outcome = await startDownload(result.current.download)

    expect(outcome).toBeInstanceOf(StaleAccessError)
    expect(payload).toHaveBeenCalledTimes(1)
    expect(saveBlob).not.toHaveBeenCalled()
  })

  it("does not save bytes that arrive after the component unmounted", async () => {
    const { result, unmount } = await mountWithCachedBlob()
    const late = pendingResponse(false)
    payload.mockImplementation(late.call)

    const settled = startDownload(result.current.download)
    await waitFor(() => expect(late.call).toHaveBeenCalled())
    const signal = late.call.mock.calls[0][3]
    unmount()
    expect(signal?.aborted).toBe(true)
    late.release()

    expect(await settled).toBeInstanceOf(StaleAccessError)
    expect(saveBlob).not.toHaveBeenCalled()
  })

  it("does not save bytes that arrive after the viewer changed", async () => {
    const { result } = await mountWithCachedBlob()
    const late = pendingResponse(false)
    payload.mockImplementation(late.call)

    const settled = startDownload(result.current.download)
    await waitFor(() => expect(late.call).toHaveBeenCalled())
    act(() => useAuthStore.setState({ user: { id: "admin-b", role: "admin" } as unknown as AuthUser }))
    late.release()

    expect(await settled).toBeInstanceOf(StaleAccessError)
    expect(saveBlob).not.toHaveBeenCalled()
  })

  it("aborts and saves nothing when access is revoked mid-download", async () => {
    const { result } = await mountWithCachedBlob()
    const late = pendingResponse(true)
    payload.mockImplementation(late.call)

    const settled = startDownload(result.current.download)
    await waitFor(() => expect(late.call).toHaveBeenCalled())
    act(() => purgeTrajectoryAccess(client, "signed_out"))

    expect(late.call.mock.calls[0][3]?.aborted).toBe(true)
    expect(await settled).toBeInstanceOf(StaleAccessError)
    expect(saveBlob).not.toHaveBeenCalled()
    expect(cachedTrajectoryData()).toEqual([])
  })

  it("does not save the previous payload after the inspector moved to another one", async () => {
    const { result, rerender } = await mountWithCachedBlob()
    const late = pendingResponse(false)
    payload.mockImplementationOnce(late.call)

    const settled = startDownload(result.current.download)
    await waitFor(() => expect(late.call).toHaveBeenCalled())
    payload.mockResolvedValue({ blob: blob("other"), filename: null })
    rerender({ sessionId: "ses_b", throughSeq: "12", payloadId: "pay_other" })
    late.release()

    expect(await settled).toBeInstanceOf(StaleAccessError)
    expect(saveBlob).not.toHaveBeenCalled()
  })
})

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { trajectoryApi } from "../api/endpoints"
import { useOpenPayloadLink } from "./useOpenPayloadLink"

vi.mock("../api/endpoints", () => ({
  trajectoryApi: { payloadLink: vi.fn(), payload: vi.fn(), events: vi.fn(), checkpoint: vi.fn() },
}))

const payloadLink = vi.mocked(trajectoryApi.payloadLink)
let client: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function fakeWindow() {
  return { closed: false, location: { replace: vi.fn() }, close: vi.fn() } as unknown as Window
}

describe("useOpenPayloadLink", () => {
  beforeEach(() => {
    client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    useAuthStore.setState({ user: { id: "admin-1", username: "ops", role: "admin" } as AuthUser } as never)
    payloadLink.mockReset()
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("opens the window first, then points it at the signed link the server issued", async () => {
    const target = fakeWindow()
    const open = vi.spyOn(window, "open").mockReturnValue(target)
    payloadLink.mockResolvedValueOnce({
      payload_id: "pay_vid", url: "https://bucket.public.test/assets/u/v.mp4?Expires=1", expires_in: 600,
      expires_at: "2026-10-13T00:10:00+00:00", media_type: "video/mp4", size_bytes: 123, name: null,
    })
    const { result } = renderHook(() => useOpenPayloadLink("ses_b", "37", "pay_vid"), { wrapper })
    let outcome
    await act(async () => {
      outcome = await result.current.open({ filename: "成片.mp4" })
    })
    expect(open).toHaveBeenCalledWith("", "_blank", "noopener,noreferrer")
    expect(payloadLink).toHaveBeenCalledWith("ses_b", "pay_vid", "37",
      expect.objectContaining({ download: undefined, name: "成片.mp4" }))
    expect(target.location.replace).toHaveBeenCalledWith("https://bucket.public.test/assets/u/v.mp4?Expires=1")
    expect(outcome).toEqual({ opened: true, expiresIn: 600 })
  })

  it.each([
    [new ApiError(409, "trajectory_payload_not_linkable", "copy"), "not_linkable"],
    [new ApiError(410, "trajectory_content_deleted", "gone"), "deleted"],
    [new ApiError(404, "not_found", "early"), "pending"],
  ])("closes the window and reports why when the server refuses (%s)", async (error, reason) => {
    const target = fakeWindow()
    vi.spyOn(window, "open").mockReturnValue(target)
    payloadLink.mockRejectedValueOnce(error)
    const { result } = renderHook(() => useOpenPayloadLink("ses_b", "37", "pay_vid"), { wrapper })
    let outcome
    await act(async () => {
      outcome = await result.current.open()
    })
    expect(outcome).toEqual({ opened: false, reason })
    expect(target.close).toHaveBeenCalled()
    expect(target.location.replace).not.toHaveBeenCalled()
  })
})

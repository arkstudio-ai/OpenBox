import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http, request, requestBlob } from "./http"
import { useAuthStore } from "./auth-store"
import { useWorkspaceStore } from "./workspace-store"
import type { AuthUser } from "@/shared/types/api"

const user = { id: "payer", username: "payer" } as AuthUser

beforeEach(() => {
  useAuthStore.getState().setAuth("expired-token", user)
  useWorkspaceStore.setState({ currentId: "original-workspace" })
})

afterEach(() => {
  vi.unstubAllGlobals()
  useAuthStore.getState().clearAuth()
  useWorkspaceStore.setState({ currentId: null })
})

describe("request scope during authentication refresh", () => {
  it.each([false, true])("retains the workspace through refresh (blob: %s)", async (blob) => {
    const fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.endsWith("/api/auth/refresh")) {
        useWorkspaceStore.setState({ currentId: "other-workspace" })
        return Response.json({ access_token: "refreshed-token" })
      }
      if (url.endsWith("/api/auth/me")) return Response.json(user)
      const calls = fetch.mock.calls.filter(([path]) => path === "/api/billing/orders")
      return calls.length === 1 ? new Response(null, { status: 401 }) : Response.json({ id: "order" })
    })
    vi.stubGlobal("fetch", fetch)
    if (blob) await requestBlob("/api/billing/orders")
    else await http.post("/api/billing/orders", { amount_fen: 1000 })
    const calls = fetch.mock.calls.filter(([path]) => path === "/api/billing/orders")
    expect(calls).toHaveLength(2)
    for (const [, options] of calls)
      expect(new Headers(options.headers).get("X-Workspace-Id")).toBe("original-workspace")
    expect(new Headers(calls[1][1].headers).get("Authorization")).toBe("Bearer refreshed-token")
    if (!blob) expect(calls[1][1].body).toBe(calls[0][1].body)
  })

  it("does not replay a payment as a different user after refresh", async () => {
    const fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.endsWith("/api/auth/refresh")) return Response.json({ access_token: "another-user-token" })
      if (url.endsWith("/api/auth/me")) return Response.json({ ...user, id: "another-user" })
      return new Response(null, { status: 401 })
    })
    vi.stubGlobal("fetch", fetch)
    await expect(http.post("/api/billing/orders", {})).rejects.toMatchObject({ status: 401 })
    expect(fetch.mock.calls.filter(([path]) => path === "/api/billing/orders")).toHaveLength(1)
  })

  it("honors an explicitly captured workspace header", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({}))
    vi.stubGlobal("fetch", fetch)
    await request("/api/billing/balance", {
      headers: new Headers({ "x-workspace-id": "captured-workspace" }),
    })
    expect(new Headers(fetch.mock.calls[0][1].headers).get("X-Workspace-Id")).toBe("captured-workspace")
  })
})

describe("refusals", () => {
  it("carry the answer's Retry-After", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json(
          { detail: { code: "trajectory_read_busy", message: "busy" } },
          { status: 429, headers: { "Retry-After": "1" } },
        ),
      )
      .mockResolvedValueOnce(Response.json({ detail: "gone" }, { status: 410 }))
    vi.stubGlobal("fetch", fetch)
    await expect(http.get("/api/admin/trajectories/sessions")).rejects.toMatchObject({
      status: 429,
      code: "trajectory_read_busy",
      retryAfter: "1",
    })
    await expect(http.get("/api/admin/trajectories/sessions")).rejects.toMatchObject({ status: 410, retryAfter: null })
  })
})

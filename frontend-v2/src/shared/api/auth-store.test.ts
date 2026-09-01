import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { refreshAccessToken, useAuthStore } from "./auth-store"

const defaultUser = { id: "default", username: "default", role: "admin" }

describe("refreshAccessToken", () => {
  beforeEach(() => {
    useAuthStore.setState({
      accessToken: null,
      user: null,
      isAuthenticated: false,
      isLoading: true,
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("enters a backend-confirmed single-user deployment without a login form", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ mode: "single_user", user: defaultUser }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
    vi.stubGlobal("fetch", fetchMock)

    const token = await refreshAccessToken()

    expect(token).toBe("openbox-single-user")
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/auth/bootstrap",
      expect.objectContaining({ credentials: "include" }),
    )
    expect(useAuthStore.getState()).toMatchObject({
      accessToken: "openbox-single-user",
      user: defaultUser,
      isAuthenticated: true,
      isLoading: false,
    })
  })

  it("remains signed out when the backend reports multi-user authentication", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(null, { status: 401 }))
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ mode: "multi_user" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
    )

    expect(await refreshAccessToken()).toBeNull()
    expect(useAuthStore.getState()).toMatchObject({
      accessToken: null,
      user: null,
      isAuthenticated: false,
      isLoading: false,
    })
  })
})

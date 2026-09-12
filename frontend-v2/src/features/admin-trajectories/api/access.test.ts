import { QueryClient } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { useTrajectoryAccess } from "../stores/access"
import { useTrajectoryView } from "../stores/view"
import {
  bindTrajectoryAccess,
  forgetTarget,
  purgeTrajectoryAccess,
  trackRequest,
  StaleAccessError,
  unbindTrajectoryAccess,
} from "./access"
import { trajectoryKeys } from "./keys"
import { acquireSync, activeSyncCount } from "./registry"

vi.mock("./endpoints", () => ({
  trajectoryApi: {
    events: vi.fn(() => new Promise(() => undefined)),
    checkpoint: vi.fn(() => new Promise(() => undefined)),
  },
}))

const admin = (id = "admin-a") => ({ id, username: id, role: "admin" }) as unknown as AuthUser
const member = (id = "admin-a") => ({ id, username: id, role: "user" }) as unknown as AuthUser

let client: QueryClient

function seedCache() {
  client.setQueryData(trajectoryKeys.header("admin-a#0", "ses_b", "live"), { title: "secret" })
  client.setQueryData(["workspace", "admin-a"], { kept: true })
}

const cachedTrajectoryQueries = () => client.getQueryCache().findAll({ queryKey: trajectoryKeys.root }).length

beforeEach(() => {
  client = new QueryClient()
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  useAuthStore.setState({ accessToken: "t", user: admin(), isAuthenticated: true, isLoading: false })
  bindTrajectoryAccess(client)
  seedCache()
})

afterEach(() => {
  unbindTrajectoryAccess()
  client.clear()
  useAuthStore.setState({ accessToken: null, user: null, isAuthenticated: false, isLoading: false })
})

describe("identity changes", () => {
  it("purges on sign-out even after a transient refresh blank", () => {
    useAuthStore.setState({ user: null, isAuthenticated: true }) // refresh in progress
    expect(cachedTrajectoryQueries()).toBe(1)
    useAuthStore.setState({ user: null, isAuthenticated: false })
    expect(cachedTrajectoryQueries()).toBe(0)
    expect(useTrajectoryAccess.getState().denied?.reason).toBe("signed_out")
    expect(client.getQueryData(["workspace", "admin-a"])).toEqual({ kept: true })
  })

  it("purges when the blank settles into a non-admin account", () => {
    useAuthStore.setState({ user: null, isAuthenticated: true })
    useAuthStore.setState({ user: member("someone-else"), isAuthenticated: true })
    expect(cachedTrajectoryQueries()).toBe(0)
    expect(useTrajectoryAccess.getState().denied?.reason).toBe("identity_changed")
  })

  it("purges when the same account loses the admin role", () => {
    useAuthStore.setState({ user: member("admin-a") })
    expect(cachedTrajectoryQueries()).toBe(0)
    expect(useTrajectoryAccess.getState().denied?.reason).toBe("role_changed")
  })

  it("keeps data across a refresh that settles on the same admin", () => {
    const epoch = useTrajectoryAccess.getState().epoch
    useAuthStore.setState({ user: null, isAuthenticated: true })
    useAuthStore.setState({ user: admin("admin-a"), isAuthenticated: true })
    expect(cachedTrajectoryQueries()).toBe(1)
    expect(useTrajectoryAccess.getState()).toMatchObject({ epoch, denied: null })
  })

  it("clears but unlocks for a different admin under a new epoch", () => {
    const epoch = useTrajectoryAccess.getState().epoch
    useAuthStore.setState({ user: admin("admin-b") })
    expect(cachedTrajectoryQueries()).toBe(0)
    expect(useTrajectoryAccess.getState()).toMatchObject({ epoch: epoch + 1, denied: null })
  })

  it("unlocks a latched denial when an admin signs in again", () => {
    useAuthStore.setState({ user: null, isAuthenticated: false })
    expect(useTrajectoryAccess.getState().denied).not.toBeNull()
    useAuthStore.setState({ user: admin("admin-a"), isAuthenticated: true })
    expect(useTrajectoryAccess.getState().denied).toBeNull()
  })
})

describe("refusals", () => {
  it("latches and clears when any trajectory query is refused, while the auth store still says admin", async () => {
    await client
      .fetchQuery({
        queryKey: trajectoryKeys.sessions("admin-a#0", ""),
        queryFn: () => Promise.reject(new ApiError(403, "HTTP_403", "Forbidden")),
        retry: false,
      })
      .catch(() => undefined)
    expect(useTrajectoryAccess.getState().denied).toEqual({ reason: "forbidden", status: 403 })
    expect(cachedTrajectoryQueries()).toBe(0)
  })

  it("leaves other features' refusals alone", async () => {
    await client
      .fetchQuery({
        queryKey: ["billing"],
        queryFn: () => Promise.reject(new ApiError(403, "HTTP_403", "x")),
        retry: false,
      })
      .catch(() => undefined)
    expect(useTrajectoryAccess.getState().denied).toBeNull()
    expect(cachedTrajectoryQueries()).toBe(1)
  })

  it("stops every stream engine and resets the view", () => {
    acquireSync("admin-a", "ses_b")
    useTrajectoryView.getState().bindTarget("admin-a ses_b", { playhead: "7", record: "tool:x" })
    purgeTrajectoryAccess(client)
    expect(activeSyncCount()).toBe(0)
    expect(useTrajectoryView.getState()).toMatchObject({
      targetKey: null,
      playhead: null,
      selectedRecordId: null,
    })
  })

  it("refuses to dispatch once access is latched or the role is gone", () => {
    expect(() => trackRequest().check()).not.toThrow()
    useTrajectoryAccess.getState().revoke("forbidden", 403)
    expect(() => trackRequest().check()).toThrow(StaleAccessError)
    useTrajectoryAccess.setState({ denied: null })
    useAuthStore.setState({ user: member("admin-a") })
    expect(() => trackRequest().check()).toThrow(StaleAccessError)
  })

  it("aborts tracked requests and marks their late results stale", () => {
    const request = trackRequest()
    purgeTrajectoryAccess(client)
    expect(request.signal.aborted).toBe(true)
    expect(() => request.check()).toThrow(StaleAccessError)
  })
})

describe("forgetting a deleted target", () => {
  it("drops that session's cache and view but keeps other sessions", () => {
    client.setQueryData(trajectoryKeys.header("admin-a#0", "ses_other", "live"), { title: "other" })
    useTrajectoryView.getState().bindTarget("admin-a ses_b")
    forgetTarget(client, "ses_b")
    expect(client.getQueryData(trajectoryKeys.header("admin-a#0", "ses_b", "live"))).toBeUndefined()
    expect(client.getQueryData(trajectoryKeys.header("admin-a#0", "ses_other", "live"))).toEqual({
      title: "other",
    })
    expect(useTrajectoryView.getState().targetKey).toBeNull()
  })
})

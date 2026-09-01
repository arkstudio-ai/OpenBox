// Auth token state. Access token lives in memory only; the refresh token is
// an HttpOnly cookie owned by the backend (ported from v1 — keep it this way).
import { create } from "zustand"
import { env } from "@/shared/config/env"
import type { AuthUser } from "@/shared/types/api"

interface AuthState {
  accessToken: string | null
  user: AuthUser | null
  isAuthenticated: boolean
  isLoading: boolean
  setAuth: (token: string, user: AuthUser | null) => void
  clearAuth: () => void
  setLoading: (loading: boolean) => void
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  user: null,
  isAuthenticated: false,
  isLoading: true, // stays true until the first refresh attempt settles
  setAuth: (token, user) => set({ accessToken: token, user, isAuthenticated: true, isLoading: false }),
  clearAuth: () => set({ accessToken: null, user: null, isAuthenticated: false, isLoading: false }),
  setLoading: (loading) => set({ isLoading: loading }),
}))

// ── Refresh with mutex: concurrent 401s trigger exactly one refresh ──
let refreshPromise: Promise<string | null> | null = null
const SINGLE_USER_ACCESS_TOKEN = "openbox-single-user"

export function refreshAccessToken(): Promise<string | null> {
  refreshPromise ??= doRefresh().finally(() => {
    refreshPromise = null
  })
  return refreshPromise
}

async function doRefresh(): Promise<string | null> {
  try {
    const resp = await fetch(`${env.apiBase}/api/auth/refresh`, {
      method: "POST",
      credentials: "include",
    })
    if (!resp.ok) {
      // Desktop/single-user deployments intentionally omit all credential
      // routes, so /refresh is a 404 rather than a missing-cookie 401.  The
      // public bootstrap contract is authoritative and returns user data only
      // when authentication is disabled; SaaS deployments report multi_user.
      const bootstrapResp = await fetch(`${env.apiBase}/api/auth/bootstrap`, {
        credentials: "include",
      })
      if (bootstrapResp.ok) {
        const bootstrap = (await bootstrapResp.json()) as {
          mode: "single_user" | "multi_user"
          user?: AuthUser
        }
        if (bootstrap.mode === "single_user" && bootstrap.user) {
          const user = bootstrap.user
          useAuthStore.getState().setAuth(SINGLE_USER_ACCESS_TOKEN, user)
          return SINGLE_USER_ACCESS_TOKEN
        }
      }
      useAuthStore.getState().clearAuth()
      return null
    }
    const data = (await resp.json()) as { access_token: string }
    const token = data.access_token
    const meResp = await fetch(`${env.apiBase}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    const user = meResp.ok ? ((await meResp.json()) as AuthUser) : null
    useAuthStore.getState().setAuth(token, user)
    return token
  } catch {
    useAuthStore.getState().clearAuth()
    return null
  }
}

// Auth mutations + the shared post-sign-in completion flow. Components never
// fetch directly — they call these hooks (ENGINEERING_SPEC §7).
import { useCallback } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useLocation, useNavigate } from "react-router"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useAppearanceStore } from "@/shared/appearance/store"
import { paths } from "@/shared/router/paths"
import type { AuthUser, UserPreferences } from "@/shared/types/api"
import {
  beginLogtoLogin,
  getLogtoConfig,
  takeReturnPath,
  type LogtoConfig,
  type LogtoResult,
  type SsoScreen,
} from "@/features/auth/lib/logto"

export interface LoginBody {
  username: string
  password: string
}
export interface RegisterBody {
  username: string
  password: string
  email?: string
}
export interface AuthResponse {
  access_token: string
  token_type?: string
  user: AuthUser
}

export function useLogin() {
  return useMutation({
    mutationFn: (body: LoginBody) => http.post<AuthResponse>("/api/auth/login", body),
  })
}

export function useRegister() {
  return useMutation({
    mutationFn: (body: RegisterBody) => http.post<AuthResponse>("/api/auth/register", body),
  })
}

/** Public Logto config, or null when SSO is off. Cached — config rarely moves. */
export function useLogtoConfig() {
  return useQuery<LogtoConfig | null>({
    queryKey: ["logto-config"],
    queryFn: getLogtoConfig,
    staleTime: Infinity,
    retry: false,
  })
}

/**
 * Starts a sign-in wherever the deployment keeps its identities.
 *
 * With Logto configured it is the only door, so the local username/password
 * pages are reached only as the fallback for a deployment without one —
 * turning Logto off must not lock everybody out. The same fallback catches a
 * redirect that fails to start, which would otherwise be a dead end.
 */
export function useSsoEntry(screen: SsoScreen = "sign_in") {
  const navigate = useNavigate()
  const { data: logto } = useLogtoConfig()
  const local = screen === "register" ? paths.register : paths.login

  return useCallback(() => {
    if (!logto) {
      navigate(local)
      return
    }
    beginLogtoLogin(logto, { firstScreen: screen }).catch(() => navigate(local))
  }, [logto, navigate, local, screen])
}

/**
 * Post-sign-in flow shared by login, register and the SSO callback:
 * seed the auth store, hydrate appearance from server prefs (best-effort),
 * then route into the app (honouring a `from` redirect target).
 */
export function useCompleteAuth() {
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from

  return useCallback(
    async (result: AuthResponse | LogtoResult) => {
      useAuthStore.getState().setAuth(result.access_token, result.user)
      try {
        const prefs = await http.get<UserPreferences>("/api/auth/me/preferences")
        useAppearanceStore.getState().hydrateFromServer(prefs)
      } catch {
        // Appearance is best-effort; a prefs failure must not block sign-in.
      }
      // `from` is empty after an SSO round trip; the stashed copy carries it.
      navigate(from ?? takeReturnPath() ?? paths.app, { replace: true })
    },
    [navigate, from],
  )
}

// Auth mutations + the shared post-sign-in completion flow. Components never
// fetch directly — they call these hooks (ENGINEERING_SPEC §7).
import { useCallback } from "react"
import i18n from "@/shared/i18n"
import { recordLegalConsent } from "@/shared/legal/consent"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useLocation, useNavigate } from "react-router"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useAppearanceStore } from "@/shared/appearance/store"
import { paths } from "@/shared/router/paths"
import type { AuthUser, UserPreferences } from "@/shared/types/api"
import { takeReturnPath, type LogtoResult, type SsoScreen } from "@/features/auth/lib/logto"
import { getLogtoConfig, type LogtoConfig } from "@/shared/api/logto"

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

/** Always show the local policy notice before handing control to hosted SSO. */
export function useSsoEntry(screen: SsoScreen = "sign_in") {
  const navigate = useNavigate()
  const local = screen === "register" ? paths.register : paths.login
  return useCallback(() => navigate(local), [navigate, local])
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
      await recordLegalConsent(i18n.language, result.access_token)
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

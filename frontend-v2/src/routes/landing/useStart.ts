import { useCallback } from "react"
import { useNavigate } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths } from "@/shared/router/paths"
import { useSsoEntry, type SsoScreen } from "@/features/auth"

/**
 * Landing CTAs: signed-in visitors jump into the app, everyone else goes to
 * Logto. "Get started" opens its sign-up screen, "sign in" its sign-in one.
 */
export function useStart(screen: SsoScreen = "register") {
  const navigate = useNavigate()
  const authed = useAuthStore((s) => s.isAuthenticated)
  const startSso = useSsoEntry(screen)

  return useCallback(() => {
    if (authed) {
      navigate(paths.app)
      return
    }
    startSso()
  }, [navigate, authed, startSso])
}

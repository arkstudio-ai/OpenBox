import { useEffect, useRef, useState, type ReactNode } from "react"
import { useLocation } from "react-router"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { useLogtoConfig } from "@/features/auth/api/auth"
import { beginLogtoLogin, rememberReturnPath, type SsoScreen } from "@/features/auth/lib/logto"

/**
 * Hands the sign-in and sign-up routes straight to Logto.
 *
 * Every way in lands on one of these two routes — the landing CTAs, the
 * `RequireAuth` bounce, a bookmarked /login — so redirecting here closes the
 * local forms without hunting down each entry point. They stay mounted
 * underneath as the fallback for a deployment with no Logto configured, and
 * for a redirect that fails to start: a spinner with no way forward would be
 * worse than the password box.
 */
export function SsoEntry({ screen, children }: { screen: SsoScreen; children: ReactNode }) {
  const { t } = useTranslation("auth")
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from
  const { data: logto, isLoading } = useLogtoConfig()
  const [failed, setFailed] = useState(false)
  const started = useRef(false)

  useEffect(() => {
    // StrictMode invokes effects twice; the redirect must fire once.
    if (started.current || !logto || failed) return
    started.current = true
    rememberReturnPath(from)
    beginLogtoLogin(logto, { firstScreen: screen }).catch(() => setFailed(true))
  }, [logto, screen, failed, from])

  if (isLoading || (logto && !failed)) {
    return (
      <div className="flex flex-col items-center gap-4 py-8">
        <Spinner className="size-6" />
        <span className="text-sm text-n700">{t("ssoRedirecting")}</span>
      </div>
    )
  }
  return <>{children}</>
}

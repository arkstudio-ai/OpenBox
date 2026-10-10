import { useState, type ReactNode } from "react"
import { useLocation } from "react-router"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { LegalConsent } from "@/shared/legal/LegalLinks"
import { stageLegalConsent } from "@/shared/legal/consent"
import { useLogtoConfig } from "@/features/auth/api/auth"
import { beginLogtoLogin, rememberReturnPath, type SsoScreen } from "@/features/auth/lib/logto"

/** Read and explicitly accept the current policies before opening hosted SSO. */
export function SsoEntry({ screen, children }: { screen: SsoScreen; children: ReactNode }) {
  const { t } = useTranslation(["auth", "legal"])
  const location = useLocation()
  const { data: logto, isLoading } = useLogtoConfig()
  const [accepted, setAccepted] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const start = async () => {
    if (!accepted || !logto || busy) return
    setBusy(true)
    try {
      stageLegalConsent()
      rememberReturnPath((location.state as { from?: string } | null)?.from)
      await beginLogtoLogin(logto, { firstScreen: screen })
    } catch {
      setFailed(true)
      setBusy(false)
    }
  }
  if (isLoading) return <Spinner className="mx-auto my-8 size-6" />
  if (!logto || failed) return <>{children}</>
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-3xl">{t(screen === "register" ? "registerTitle" : "loginTitle")}</h1>
      <p className="text-n700 text-sm">{t(screen === "register" ? "registerBody" : "loginBody")}</p>
      <LegalConsent accepted={accepted} onChange={setAccepted} />
      <button
        type="button"
        onClick={() => void start()}
        disabled={!accepted || busy}
        className="bg-ink text-bg h-11 rounded-lg text-sm disabled:opacity-50"
      >
        {t(busy ? "ssoRedirecting" : "sso")}
      </button>
    </div>
  )
}

import { LegalConsent } from "@/shared/legal/LegalLinks"
import { stageLegalConsent } from "@/shared/legal/consent"
import { useState, type FormEvent } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { useLogin, useCompleteAuth } from "@/features/auth/api/auth"
import { useAuthErrorMessage } from "@/features/auth/lib/errors"
import { TextField, PasswordField } from "@/features/auth/components/AuthFields"
import { SsoButton } from "@/features/auth/components/SsoButton"

/** Card content for the login route (AuthShell supplies the card shell). */
export function LoginForm() {
  const { t } = useTranslation(["auth", "legal"])
  const login = useLogin()
  const complete = useCompleteAuth()
  const toMessage = useAuthErrorMessage()

  const [account, setAccount] = useState("")
  const [password, setPassword] = useState("")
  const [remember, setRemember] = useState(true)
  const [accepted, setAccepted] = useState(false)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (!accepted) {
      setError(t("legal:consentRequired"))
      return
    }
    if (!account.trim() || !password) {
      setError(t("errors.required"))
      return
    }
    setError("")
    setBusy(true)
    try {
      stageLegalConsent()
      const result = await login.mutateAsync({ username: account.trim(), password })
      await complete(result)
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col">
      <h1 className="text-3xl">{t("loginTitle")}</h1>
      <p className="text-n700 mt-2 text-sm">{t("loginBody")}</p>

      <div className="mt-6">
        <TextField
          label={t("accountLabel")}
          value={account}
          onChange={setAccount}
          placeholder={t("accountPlaceholder")}
          autoComplete="username"
        />
        <PasswordField
          label={t("pwLabel")}
          value={password}
          onChange={setPassword}
          placeholder={t("pwPlaceholder")}
          autoComplete="current-password"
        />
      </div>

      <div className="mt-3.5 flex items-center gap-2">
        <button
          type="button"
          role="checkbox"
          aria-checked={remember}
          aria-label={t("remember")}
          onClick={() => setRemember((r) => !r)}
          className={cn(
            "text-2xs flex size-4 flex-none items-center justify-center rounded-sm border leading-none",
            remember ? "border-ink bg-ink text-bg" : "border-n400 text-transparent",
          )}
        >
          ✓
        </button>
        <button type="button" onClick={() => setRemember((r) => !r)} className="text-n700 text-xs">
          {t("remember")}
        </button>
      </div>

      <LegalConsent accepted={accepted} onChange={setAccepted} />
      {error && <p className="text-2xs text-danger mt-3">{error}</p>}

      <button
        type="submit"
        disabled={busy}
        className="bg-ink text-bg hover:bg-a800 mt-5 h-11 rounded-lg text-sm font-medium disabled:opacity-60"
      >
        {busy ? t("signingIn") : t("signInBtn")}
      </button>

      {/* Renders nothing unless the server has Logto configured. */}
      <SsoButton accepted={accepted} />

      <Link to={paths.register} className="text-a700 hover:text-ink mt-3.5 text-xs">
        {t("noAccount")}
      </Link>
    </form>
  )
}

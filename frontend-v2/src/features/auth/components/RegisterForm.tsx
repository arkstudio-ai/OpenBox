import { LegalConsent } from "@/shared/legal/LegalLinks"
import { stageLegalConsent } from "@/shared/legal/consent"
import { useState, type FormEvent } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import { useRegister, useCompleteAuth } from "@/features/auth/api/auth"
import { useAuthErrorMessage } from "@/features/auth/lib/errors"
import { TextField, PasswordField } from "@/features/auth/components/AuthFields"

/** Card content for the register route (AuthShell supplies the card shell). */
export function RegisterForm() {
  const { t } = useTranslation(["auth", "legal"])
  const register = useRegister()
  const complete = useCompleteAuth()
  const toMessage = useAuthErrorMessage()

  const [account, setAccount] = useState("")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [confirm, setConfirm] = useState("")
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
    if (password !== confirm) {
      setError(t("errors.pwMismatch"))
      return
    }
    setError("")
    setBusy(true)
    try {
      stageLegalConsent()
      const result = await register.mutateAsync({
        username: account.trim(),
        password,
        email: email.trim() || undefined,
      })
      await complete(result)
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col">
      <h1 className="text-3xl">{t("registerTitle")}</h1>
      <p className="text-n700 mt-2 text-sm">{t("registerBody")}</p>

      <div className="mt-6">
        <TextField
          label={t("accountLabel")}
          value={account}
          onChange={setAccount}
          placeholder={t("accountPlaceholder")}
          autoComplete="username"
        />
        <TextField
          label={t("emailLabel")}
          value={email}
          onChange={setEmail}
          placeholder={t("emailPlaceholder")}
          type="email"
          autoComplete="email"
        />
        <PasswordField
          label={t("pwLabel")}
          value={password}
          onChange={setPassword}
          placeholder={t("pwPlaceholder")}
          autoComplete="new-password"
        />
        <PasswordField
          label={t("pwConfirmLabel")}
          value={confirm}
          onChange={setConfirm}
          placeholder={t("pwConfirmPlaceholder")}
          autoComplete="new-password"
        />
      </div>

      <LegalConsent accepted={accepted} onChange={setAccepted} />
      {error && <p className="text-2xs text-danger mt-3">{error}</p>}

      <button
        type="submit"
        disabled={busy}
        className="bg-ink text-bg hover:bg-a800 mt-5 h-11 rounded-lg text-sm font-medium disabled:opacity-60"
      >
        {busy ? t("signingIn") : t("registerBtn")}
      </button>

      <Link to={paths.login} className="text-a700 hover:text-ink mt-3.5 text-xs">
        {t("haveAccount")}
      </Link>
    </form>
  )
}

import { CloudOff } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"

interface Props {
  target: SkillGroup
  busy: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: () => void
}

/**
 * Taking one's own skill off the shelf, confirmed first.
 *
 * Reversible, and the dialog says so: withdrawal keeps the release the store
 * held, so the author can put it back without rebuilding anything. What it
 * does not do is reach into the sandboxes of people who already installed it —
 * a promise the copy makes explicitly, because the opposite is what someone
 * clicking "withdraw" tends to assume.
 */
export function WithdrawSkillDialog({ target, busy, error, onCancel, onConfirm }: Props) {
  const { t } = useTranslation("skills")

  // The scrim colour has to come from a theme token: `--color-*: initial` in
  // tokens.css clears Tailwind's default palette, so `bg-black` compiles to
  // nothing at all — an overlay that swallows clicks while dimming nothing.
  return (
    <div
      className="bg-n900/30 fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("withdraw.title")}
    >
      <div className="border-hair bg-card w-full max-w-[440px] rounded-2xl border p-5 shadow-xl">
        <span className="bg-a100 text-a800 mb-3 flex size-10 items-center justify-center rounded-xl">
          <CloudOff size={19} aria-hidden />
        </span>
        <h2 className="text-ink text-base font-medium">{t("withdraw.title")}</h2>
        <p className="text-n700 mt-1 text-sm leading-6">{t("withdraw.body", { name: target.name })}</p>
        <p className="bg-hairsoft/60 text-n600 mt-3 rounded-lg px-3 py-2 text-xs leading-5">
          {t("withdraw.notice")}
        </p>
        {error ? (
          <p className="bg-dangersoft text-danger mt-3 rounded-lg px-3 py-2 text-xs leading-5">{error}</p>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="text-n700 hover:bg-hairsoft rounded-full px-3.5 py-1.5 text-sm disabled:opacity-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="bg-ink text-bg rounded-full px-3.5 py-1.5 text-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? t("withdraw.working") : t("withdraw.confirm")}
          </button>
        </div>
      </div>
    </div>
  )
}

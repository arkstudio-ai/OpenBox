import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useAppearanceStore } from "@/shared/appearance/store"
import { ApiError } from "@/shared/api/http"
import { toast } from "@/shared/ui/Toast"

/** What the person wants to call their assistant: one short name, kept with the account and
 *  used by the web app, the mobile app, the assistant's own replies and the phone front desk. */
export function AssistantPage() {
  const { t } = useTranslation("settings")
  const current = useAppearanceStore((s) => s.assistantName)
  const setAssistantName = useAppearanceStore((s) => s.setAssistantName)
  // null: the field shows the stored name (which arrives with the account's preferences after
  // sign-in, and changes when the assistant renames itself); a string once the person types.
  const [typed, setTyped] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const draft = typed ?? current
  const changed = draft.trim() !== current

  const save = async (name: string) => {
    setSaving(true)
    try {
      const kept = await setAssistantName(name)
      setTyped(null)
      toast("success", kept ? t("assistant.saved", { name: kept }) : t("assistant.resetDone"))
    } catch (error) {
      const invalid = error instanceof ApiError && error.status === 422
      toast("error", invalid ? t("assistant.invalid") : t("assistant.saveFailed"))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault()
        if (changed && !saving) void save(draft)
      }}
    >
      <label className="block space-y-2">
        <span className="text-ink text-sm font-medium">{t("assistant.nameLabel")}</span>
        <input
          type="text"
          value={draft}
          maxLength={20}
          onChange={(event) => setTyped(event.target.value)}
          placeholder={t("assistant.placeholder")}
          className="border-hair bg-card text-ink placeholder:text-n600 focus:border-n400 h-10 w-full max-w-md rounded-lg border px-3 text-base outline-none"
        />
      </label>
      <div className="flex flex-wrap items-center gap-2.5">
        <button
          type="submit"
          disabled={!changed || saving}
          className="bg-ink text-bg h-9 rounded-full px-4.5 text-sm font-medium disabled:opacity-40"
        >
          {t("assistant.save")}
        </button>
        {current && (
          <button
            type="button"
            disabled={saving}
            onClick={() => void save("")}
            className="text-n700 hover:bg-hairsoft h-9 rounded-full px-3.5 text-sm disabled:opacity-40"
          >
            {t("assistant.reset")}
          </button>
        )}
      </div>
      <p className="text-n600 text-xs text-pretty">{t("assistant.note")}</p>
    </form>
  )
}

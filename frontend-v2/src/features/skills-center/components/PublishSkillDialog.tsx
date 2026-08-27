import { UploadCloud } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"

interface Props {
  target: SkillGroup
  busy: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: () => void
}

/** Publishing is an explicit public action, so it always gets a confirmation. */
export function PublishSkillDialog({ target, busy, error, onCancel, onConfirm }: Props) {
  const { t } = useTranslation("skills")
  const updating = target.publicationStatus === "published"

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t(updating ? "publish.updateTitle" : "publish.title")}
    >
      <div className="border-hair bg-card w-full max-w-[440px] rounded-2xl border p-5 shadow-xl">
        <span className="bg-a100 text-a800 mb-3 flex size-10 items-center justify-center rounded-xl">
          <UploadCloud size={19} aria-hidden />
        </span>
        <h2 className="text-ink text-base font-medium">
          {t(updating ? "publish.updateTitle" : "publish.title")}
        </h2>
        <p className="text-n700 mt-1 text-sm leading-6">
          {t(updating ? "publish.updateBody" : "publish.body", { name: target.name })}
        </p>
        <p className="bg-hairsoft/60 text-n600 mt-3 rounded-lg px-3 py-2 text-xs leading-5">
          {t("publish.publicNotice")}
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
            {busy ? t("publish.working") : t(updating ? "publish.confirmUpdate" : "publish.confirm")}
          </button>
        </div>
      </div>
    </div>
  )
}

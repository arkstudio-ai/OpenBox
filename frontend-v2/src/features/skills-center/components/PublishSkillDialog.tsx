import { UploadCloud } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import { isResubmission, listingChipFor } from "@/features/skills-center/lib/listing"

interface Props {
  target: SkillGroup
  /** Whether this deployment queues submissions for an admin to look at. */
  reviewRequired: boolean
  busy: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: () => void
}

/**
 * Publishing is an explicit public action, so it always gets a confirmation.
 *
 * What the confirmation promises depends on the deployment: with review on,
 * clicking submits to a queue and nothing is public yet; with it off, the
 * click is the publication. Both sentences live in the locale and are chosen
 * from `skill_store_review` — a dialog that guesses makes the product lie
 * about whether strangers can already read what was just uploaded.
 */
export function PublishSkillDialog({ target, reviewRequired, busy, error, onCancel, onConfirm }: Props) {
  const { t } = useTranslation("skills")
  const chip = listingChipFor(target.publicationStatus, target.listing)
  const resubmit = isResubmission(chip)
  const updating = target.publicationStatus === "published"

  const titleKey = resubmit ? "publish.resubmitTitle" : updating ? "publish.updateTitle" : "publish.title"
  const bodyKey = resubmit ? "publish.resubmitBody" : updating ? "publish.updateBody" : "publish.body"
  const confirmKey = reviewRequired
    ? "publish.confirmSubmit"
    : resubmit
      ? "publish.confirmResubmit"
      : updating
        ? "publish.confirmUpdate"
        : "publish.confirm"

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t(titleKey)}
    >
      <div className="border-hair bg-card w-full max-w-[440px] rounded-2xl border p-5 shadow-xl">
        <span className="bg-a100 text-a800 mb-3 flex size-10 items-center justify-center rounded-xl">
          <UploadCloud size={19} aria-hidden />
        </span>
        <h2 className="text-ink text-base font-medium">{t(titleKey)}</h2>
        <p className="text-n700 mt-1 text-sm leading-6">{t(bodyKey, { name: target.name })}</p>
        <p className="bg-hairsoft/60 text-n600 mt-3 rounded-lg px-3 py-2 text-xs leading-5">
          {t(reviewRequired ? "publish.reviewNotice" : "publish.publicNotice")}
        </p>
        {/* Re-submitting after a refusal or a delisting: repeat what was said
            about it, so the fix can be checked against the reason. */}
        {resubmit && target.listingNote ? (
          <p className="bg-a100 text-n700 mt-2 rounded-lg px-3 py-2 text-xs leading-5 whitespace-pre-wrap">
            {t("mine.listingReason", { note: target.listingNote })}
          </p>
        ) : null}
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
            {busy ? t(reviewRequired ? "publish.submitting" : "publish.working") : t(confirmKey)}
          </button>
        </div>
      </div>
    </div>
  )
}

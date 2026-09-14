import { useTranslation } from "react-i18next"
import { ShieldOff } from "lucide-react"
import { DENIAL_LABELS, labelKey } from "../constants/labels"
import type { AccessDenial } from "../stores/access"

interface AccessNoticeProps {
  denial: AccessDenial
}

/** Shown in place of any trajectory content once this viewer's access was refused; nothing cached is rendered. */
export function AccessNotice({ denial }: AccessNoticeProps) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <div
      role="alert"
      data-testid="trajectory-access-denied"
      className="border-hair bg-card flex items-start gap-3 rounded-xl border p-5"
    >
      <ShieldOff size={18} aria-hidden className="text-dangerink mt-0.5 flex-none" />
      <div className="flex flex-col gap-1">
        <p className="text-ink text-sm font-medium">{t("access.title")}</p>
        <p className="text-n600 text-xs">{t(labelKey(DENIAL_LABELS, denial.reason, "access.forbidden"))}</p>
      </div>
    </div>
  )
}

import { useTranslation } from "react-i18next"
import { CompanionAvatar } from "@/shared/appearance/CompanionAvatar"
import { DEFAULT_COMPANION, useCompanionAppearance } from "@/shared/appearance/companion"
import { useAssistantNames } from "@/shared/appearance/useAssistantNames"

interface Props {
  onPick: (text: string) => void
}
export function CompanionWelcome({ onPick }: Props) {
  const { t } = useTranslation("companion")
  const name = useAssistantNames().title
  const appearance = useCompanionAppearance().data ?? DEFAULT_COMPANION
  return (
    <div className="scr flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-7 py-6 text-center">
      <CompanionAvatar appearance={appearance} className="mb-4 size-24" />
      <h2 className="text-lg font-semibold">{t("welcome.title", { name })}</h2>
      <p className="text-n700 mt-2 max-w-70 text-sm leading-6">{t("welcome.body")}</p>
      <button
        type="button"
        onClick={() => onPick(t("welcome.prompt"))}
        className="border-hair hover:bg-hairsoft mt-5 min-h-11 rounded-full border px-4 text-xs"
      >
        {t("welcome.action")}
      </button>
    </div>
  )
}

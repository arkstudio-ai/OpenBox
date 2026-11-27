import { VideoTemplateStudio } from "@/features/resources"
import { useTranslation } from "react-i18next"
import { SkillCenter } from "@/features/skills-center"

export default function SkillsRoute() {
  const { t } = useTranslation("skills")
  return (
    <div className="scr min-h-0 flex-1 overflow-auto px-6.5 pt-1.5 pb-7">
      <div className="mx-auto flex w-full max-w-[820px] flex-col gap-4.5">
        <div className="flex flex-col gap-1">
          <span className="text-2xl font-medium tracking-tight">{t("page.title")}</span>
          <span className="text-n600 text-sm">{t("page.subtitle")}</span>
        </div>
        <div>
          <VideoTemplateStudio />
        </div>
        <SkillCenter />
      </div>
    </div>
  )
}

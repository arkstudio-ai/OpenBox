import { useSearchParams } from "react-router"
import { useTranslation } from "react-i18next"
import { SkillCenter } from "@/features/skills-center"
import { MediaLibrary } from "@/features/media-templates"
import { cn } from "@/shared/lib/cn"

export default function SkillsRoute() {
  const { t } = useTranslation("skills")
  const [search, setSearch] = useSearchParams()
  const media = search.get("section") === "media"
  return (
    <div className="scr min-h-0 flex-1 overflow-auto px-6.5 pt-1.5 pb-7">
      <div className="mx-auto flex w-full max-w-[960px] flex-col gap-4.5">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-medium tracking-tight">{t("page.title")}</h1>
          {!media && <span className="text-n600 text-sm">{t("page.subtitle")}</span>}
        </div>
        <nav className="border-hair flex gap-5 border-b text-sm">
          {[["apps", "apps"], ["media", "media"]].map(([key, label]) => {
            const active = key === (media ? "media" : "apps")
            return <button key={key} aria-current={active ? "page" : undefined} type="button"
              className={cn("border-b-2 px-1 py-2.5 font-medium", active ? "border-a700 text-a700" : "border-transparent text-n600")}
              onClick={() => setSearch(key === "media" ? { section: "media" } : {}, { replace: true })}>
              {t(`mediaTemplates.${label}`)}
            </button>
          })}
        </nav>
        {media ? <MediaLibrary /> : <SkillCenter />}
      </div>
    </div>
  )
}

import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { BookOpen, ArrowUpRight } from "lucide-react"
import { paths } from "@/shared/router/paths"
import { memoryCard, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"

/** Memory management stays focused; the Wiki owns its reading and review flows. */
export function WikiPanel({ projectId }: { projectId: string }) {
  const { t } = useTranslation("wiki")
  return (
    <section
      className={memoryCard + " flex flex-wrap items-center justify-between gap-4"}
      aria-label={t("title")}
    >
      <div className="flex items-start gap-3">
        <BookOpen className="text-a700 mt-1 size-5" />
        <div>
          <h2 className="font-medium">{t("title")}</h2>
          <p className="text-n600 mt-1 text-sm leading-6">{t("entryHint")}</p>
        </div>
      </div>
      <Link className={memoryPrimary + " inline-flex items-center gap-2"} to={paths.wiki(projectId)}>
        {t("openLibrary")}
        <ArrowUpRight className="size-4" />
      </Link>
    </section>
  )
}

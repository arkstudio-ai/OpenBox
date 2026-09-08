import { useTranslation } from "react-i18next"
import { Pin } from "lucide-react"
import { StatusPill } from "@/shared/ui/StatusPill"
import type { StoreEntry } from "@/features/admin-skills/types"

/** Name column: icon, title, and the two ids an operator greps the logs with. */
export function StoreEntryCell({ entry }: { entry: StoreEntry }) {
  const { t } = useTranslation("admin-skills")
  return (
    <div className="flex items-start gap-2">
      <span aria-hidden className="text-base leading-5">
        {entry.icon || "🧩"}
      </span>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-ink font-medium">{entry.title}</span>
          {entry.featured && (
            <StatusPill tone="accent" className="gap-1">
              <Pin className="size-2.5" aria-hidden />
              {t("store.featured")}
            </StatusPill>
          )}
          {entry.is_official && <StatusPill tone="accent">{t("store.official")}</StatusPill>}
        </div>
        <div className="text-n600 text-2xs mt-0.5 truncate font-mono">
          {entry.name} · {entry.catalog_id}
        </div>
      </div>
    </div>
  )
}

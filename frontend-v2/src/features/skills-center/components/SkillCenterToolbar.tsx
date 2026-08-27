import { MessageSquarePlus, Plus, Search } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { CenterTab, KindFilter } from "@/features/skills-center/types"

const CENTER_TABS: CenterTab[] = ["mine", "store"]
const KIND_FILTERS: KindFilter[] = ["all", "skill", "mcp"]

interface Props {
  filters: { tab: CenterTab; kind: KindFilter; query: string }
  onChange: {
    tab: (value: CenterTab) => void
    kind: (value: KindFilter) => void
    query: (value: string) => void
  }
  onCreateChat: () => void
  onAdd: () => void
}

export function SkillCenterToolbar({ filters, onChange, onCreateChat, onAdd }: Props) {
  const { t } = useTranslation("skills")
  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <div className="flex gap-1">
          {CENTER_TABS.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onChange.tab(key)}
              className={`rounded-full px-3.5 py-1.5 text-sm transition-colors ${
                filters.tab === key ? "bg-ink text-bg" : "text-n700 hover:bg-hairsoft"
              }`}
            >
              {t(`tab.${key}`)}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onCreateChat}
            className="bg-ink text-bg flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm hover:opacity-90"
          >
            <MessageSquarePlus size={15} />
            {t("action.createWithChat")}
          </button>
          <button
            type="button"
            onClick={onAdd}
            className="border-hair text-ink hover:bg-hairsoft flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm"
          >
            <Plus size={15} />
            {t("action.add")}
          </button>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <div className="border-hair bg-canvas flex flex-1 items-center gap-2 rounded-xl border px-3 py-2">
          <Search size={15} className="text-n600 flex-none" aria-hidden />
          <input
            value={filters.query}
            onChange={(event) => onChange.query(event.target.value)}
            placeholder={t("searchPlaceholder")}
            className="text-ink placeholder:text-n600 min-w-0 flex-1 bg-transparent text-sm outline-none"
          />
        </div>
        <div className="flex flex-none gap-1">
          {KIND_FILTERS.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onChange.kind(key)}
              className={`rounded-full px-2.5 py-1.5 text-xs transition-colors ${
                filters.kind === key ? "bg-hairsoft text-ink" : "text-n600 hover:bg-hairsoft/60"
              }`}
            >
              {t(`filter.${key}`)}
            </button>
          ))}
        </div>
      </div>
    </>
  )
}

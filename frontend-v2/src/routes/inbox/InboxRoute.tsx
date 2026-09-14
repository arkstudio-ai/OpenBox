import { useTranslation } from "react-i18next"
import { useSearchParams } from "react-router"
import { InboxPage } from "@/features/inbox"

const CATEGORIES = ["session", "system", "notice"] as const
type Category = (typeof CATEGORIES)[number]

export default function InboxRoute() {
  const { t } = useTranslation("inbox")
  const [params] = useSearchParams()
  const raw = params.get("category")
  const initial = CATEGORIES.find((value) => value === raw) as Category | undefined
  return (
    <div className="scr min-h-0 flex-1 overflow-auto px-6.5 pt-1.5 pb-7">
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-4.5">
        <div className="flex flex-col gap-1">
          <span className="text-2xl font-medium tracking-tight">{t("title")}</span>
          <span className="text-n600 text-sm">{t("subtitle")}</span>
        </div>
        <InboxPage initialCategory={initial ?? ""} />
      </div>
    </div>
  )
}

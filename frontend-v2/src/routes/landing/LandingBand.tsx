import { useTranslation } from "react-i18next"

export function LandingBand() {
  const { t } = useTranslation("landing")
  return (
    <div className="mt-20 bg-ink px-7 py-6 text-center text-sm text-bg">
      <p className="mx-auto max-w-[720px] text-pretty">{t("band")}</p>
    </div>
  )
}

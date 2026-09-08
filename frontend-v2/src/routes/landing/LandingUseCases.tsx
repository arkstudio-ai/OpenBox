import { useTranslation } from "react-i18next"
import { LandingSection } from "./LandingSection"

interface Item {
  title: string
  body: string
}

export function LandingUseCases() {
  const { t } = useTranslation("landing")
  const items = t("useCases.items", { returnObjects: true }) as unknown as Item[]
  return (
    <LandingSection id="use-cases" eyebrow={t("useCases.eyebrow")} title={t("useCases.title")}>
      <div className="mt-8 grid grid-cols-1 gap-3.5 md:grid-cols-3">
        {items.map((c) => (
          <div key={c.title} className="flex flex-col gap-2 rounded-xl border border-hair bg-card px-5.5 py-5.5">
            <h3 className="text-lg">{c.title}</h3>
            <p className="text-pretty text-sm leading-relaxed text-n700">{c.body}</p>
          </div>
        ))}
      </div>
    </LandingSection>
  )
}

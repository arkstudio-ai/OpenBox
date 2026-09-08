import { useTranslation } from "react-i18next"
import { LandingSection } from "./LandingSection"

interface Item {
  title: string
  body: string
}

export function LandingCapabilities() {
  const { t } = useTranslation("landing")
  const items = t("capabilities.items", { returnObjects: true }) as unknown as Item[]
  return (
    <LandingSection id="capabilities" eyebrow={t("capabilities.eyebrow")} title={t("capabilities.title")}>
      <div className="mt-8 grid grid-cols-1 gap-3.5 md:grid-cols-2 lg:grid-cols-4">
        {items.map((f, i) => (
          <div key={f.title} className="flex flex-col gap-2.5 rounded-xl border border-hair bg-card px-5.5 pt-5.5 pb-6">
            <span className="self-start rounded-md bg-n200 px-2 py-0.5 font-mono text-2xs text-n600">
              {String(i + 1).padStart(2, "0")}
            </span>
            <h3 className="text-lg">{f.title}</h3>
            <p className="text-pretty text-sm leading-relaxed text-n700">{f.body}</p>
          </div>
        ))}
      </div>
    </LandingSection>
  )
}

import { useTranslation } from "react-i18next"
import { LandingSection } from "./LandingSection"

interface Item {
  q: string
  a: string
}

export function LandingFaq() {
  const { t } = useTranslation("landing")
  const items = t("faq.items", { returnObjects: true }) as unknown as Item[]
  return (
    <LandingSection id="faq" eyebrow={t("faq.eyebrow")} title={t("faq.title")}>
      <div className="mt-6 flex max-w-[760px] flex-col divide-y divide-hair border-y border-hair">
        {items.map((item) => (
          <details key={item.q} className="group py-4">
            <summary className="flex cursor-pointer list-none items-center gap-3 text-base [&::-webkit-details-marker]:hidden">
              <span className="min-w-0 flex-1">{item.q}</span>
              <span className="flex-none font-mono text-xs text-n500 transition-transform group-open:rotate-45">+</span>
            </summary>
            <p className="mt-2.5 max-w-[640px] text-pretty text-sm leading-relaxed text-n700">{item.a}</p>
          </details>
        ))}
      </div>
    </LandingSection>
  )
}

import { useTranslation } from "react-i18next"
import { useStart } from "./useStart"
import { LandingPreview } from "./LandingPreview"
import { Eyebrow } from "./LandingSection"

interface Metric {
  value: string
  label: string
}

export function LandingHero() {
  const { t } = useTranslation("landing")
  const start = useStart()
  const metrics = t("hero.metrics", { returnObjects: true }) as unknown as Metric[]

  return (
    <section className="mx-auto grid max-w-[1080px] items-center gap-12 px-7 pt-16 md:grid-cols-[1.05fr_1fr] md:pt-20">
      <div className="flex flex-col items-start">
        <Eyebrow>{t("hero.eyebrow")}</Eyebrow>
        <h1 className="mt-4 text-pretty text-hero md:text-display">{t("hero.title")}</h1>
        <p className="mt-5 max-w-[520px] text-pretty text-lg text-n700">{t("hero.lede")}</p>
        <div className="mt-7.5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={start}
            className="h-10.5 rounded-full bg-ink px-5.5 text-base text-bg hover:bg-a800"
          >
            {t("hero.primary")}
          </button>
          <a
            href="#capabilities"
            className="flex h-10.5 items-center rounded-full border border-hair bg-card px-5 text-base text-ink hover:bg-hairsoft"
          >
            {t("hero.secondary")}
          </a>
        </div>
        <dl className="mt-8 grid w-full max-w-[460px] grid-cols-2 gap-3">
          {metrics.map((m) => (
            <div key={m.value} className="flex flex-col gap-0.5 rounded-xl border border-hair bg-card px-4 py-3">
              <dt className="text-base font-semibold">{m.value}</dt>
              <dd className="text-xs text-n600">{m.label}</dd>
            </div>
          ))}
        </dl>
      </div>
      <LandingPreview />
    </section>
  )
}

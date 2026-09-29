import { useTranslation } from "react-i18next"
import { useStart } from "./useStart"
import { Eyebrow } from "./LandingSection"
import { LegalFooter } from "@/shared/legal/LegalLinks"

export function LandingOutro() {
  const { t } = useTranslation("landing")
  const start = useStart()

  return (
    <>
      <section className="mx-auto mt-23 max-w-[1080px] px-7">
        <div className="border-hair bg-card flex flex-col items-center gap-4 rounded-2xl border px-10 py-12 text-center">
          <Eyebrow>{t("final.eyebrow")}</Eyebrow>
          <h2 className="max-w-[560px] text-3xl leading-tight text-pretty">{t("final.title")}</h2>
          <button
            type="button"
            onClick={start}
            className="bg-ink text-bg hover:bg-a800 mt-3 h-11 flex-none rounded-full px-6 text-base"
          >
            {t("final.cta")}
          </button>
        </div>
      </section>

      <footer className="mx-auto mt-15 max-w-[1080px] px-7 pb-11.5">
        <LegalFooter />
      </footer>
    </>
  )
}

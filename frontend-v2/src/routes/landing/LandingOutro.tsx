import { useTranslation } from "react-i18next"
import { useStart } from "./useStart"
import { Eyebrow } from "./LandingSection"

export function LandingOutro() {
  const { t } = useTranslation("landing")
  const start = useStart()
  const links = t("footer.links", { returnObjects: true }) as unknown as {
    terms: string
    privacy: string
    contact: string
  }

  return (
    <>
      <section className="mx-auto mt-23 max-w-[1080px] px-7">
        <div className="flex flex-col items-center gap-4 rounded-2xl border border-hair bg-card px-10 py-12 text-center">
          <Eyebrow>{t("final.eyebrow")}</Eyebrow>
          <h2 className="max-w-[560px] text-pretty text-3xl leading-tight">{t("final.title")}</h2>
          <button
            type="button"
            onClick={start}
            className="mt-3 h-11 flex-none rounded-full bg-ink px-6 text-base text-bg hover:bg-a800"
          >
            {t("final.cta")}
          </button>
        </div>
      </section>

      <footer className="mx-auto mt-15 max-w-[1080px] px-7 pb-11.5">
        <div className="flex flex-col gap-3 border-t border-hair pt-5.5 md:flex-row md:items-center">
          <span className="text-xs text-n600">{t("footer.rights")}</span>
          <span className="text-xs text-n600">{t("footer.copy")}</span>
          <div className="flex gap-5 md:ms-auto">
            <span className="text-xs text-n600">{links.terms}</span>
            <span className="text-xs text-n600">{links.privacy}</span>
            <span className="text-xs text-n600">{links.contact}</span>
          </div>
        </div>
      </footer>
    </>
  )
}

import { useTranslation } from "react-i18next"
import { BrandMark, LangPill } from "@/features/auth"
import { useStart } from "./useStart"
import { EnvBadge } from "@/shared/ui/EnvBadge"

const SECTIONS = [
  { key: "capabilities", href: "#capabilities" },
  { key: "workflow", href: "#workflow" },
  { key: "useCases", href: "#use-cases" },
  { key: "faq", href: "#faq" },
] as const

/** Sticky landing header: brand, section anchors, language toggle, CTAs. */
export function LandingTopbar() {
  const { t } = useTranslation("landing")
  const signIn = useStart("sign_in")
  const start = useStart()

  return (
    <header className="sticky top-0 z-20 flex-none border-b border-hair bg-bg/85 backdrop-blur-md">
      <div className="mx-auto flex h-15.5 max-w-[1080px] items-center gap-3 px-7">
        <BrandMark dot />
        <EnvBadge />
        <nav className="hidden flex-1 justify-center gap-1 md:flex">
          {SECTIONS.map((s) => (
            <a
              key={s.key}
              href={s.href}
              className="whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm text-n700 hover:bg-hairsoft hover:text-ink"
            >
              {t(`nav.${s.key}`)}
            </a>
          ))}
        </nav>
        <div className="flex-1 md:hidden" />
        <LangPill />
        <button type="button" onClick={signIn} className="flex-none px-1.5 text-sm text-n700 hover:text-ink">
          {t("signIn")}
        </button>
        <button
          type="button"
          onClick={start}
          className="flex-none rounded-full bg-ink px-4 py-2 text-sm text-bg hover:bg-a800"
        >
          {t("start")}
        </button>
      </div>
    </header>
  )
}

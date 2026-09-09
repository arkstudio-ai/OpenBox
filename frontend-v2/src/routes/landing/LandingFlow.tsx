import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { Eyebrow } from "./LandingSection"

interface Step {
  title: string
  body: string
}

export function LandingFlow() {
  const { t } = useTranslation("landing")
  const steps = t("workflow.steps", { returnObjects: true }) as unknown as Step[]
  return (
    <section id="workflow" className="mx-auto max-w-[1080px] scroll-mt-20 px-7 pt-22">
      <div className="flex flex-col items-start gap-11 border-t border-hair pt-8.5 md:flex-row">
        <div className="flex w-full flex-none flex-col gap-3 md:w-65">
          <Eyebrow>{t("workflow.eyebrow")}</Eyebrow>
          <h2 className="text-pretty text-3xl leading-tight">{t("workflow.title")}</h2>
        </div>
        <ol className="flex min-w-0 flex-1 flex-col">
          {steps.map((s, i) => (
            <li key={s.title} className={cn("flex gap-4 py-5", i > 0 && "border-t border-hair")}>
              <span className="flex size-6 flex-none items-center justify-center rounded-full bg-ink font-mono text-2xs text-bg">
                {i + 1}
              </span>
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="text-base">{s.title}</span>
                <p className="mt-1.5 text-pretty text-sm leading-relaxed text-n700">{s.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}

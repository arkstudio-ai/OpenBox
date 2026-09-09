import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"

interface Task {
  title: string
  body: string
}

const BARS = [44, 58, 49, 72, 64, 83]

/** Static product stage: a day's operating plan plus one business signal. */
export function LandingPreview() {
  const { t } = useTranslation("landing")
  const rails = t("preview.rails", { returnObjects: true }) as unknown as string[]
  const tasks = t("preview.tasks", { returnObjects: true }) as unknown as Task[]

  return (
    <div className="overflow-hidden rounded-[20px] border border-hair bg-card shadow-float" aria-hidden="true">
      <div className="flex h-10 items-center gap-2 border-b border-hair bg-rail px-3.5">
        <span className="size-2 rounded-full bg-n300" />
        <span className="size-2 rounded-full bg-n300" />
        <span className="size-2 rounded-full bg-n300" />
        <span className="ms-2 flex items-center gap-1.5 text-xs text-n700">
          <span className="flex size-4 items-center justify-center rounded bg-ink text-2xs font-bold text-bg">b</span>
          {t("preview.title")}
        </span>
        <span className="ms-auto flex items-center gap-1.5 rounded-full bg-s100 px-2 py-0.5 text-2xs text-s700">
          <span className="size-1.5 rounded-full bg-s600" />
          {t("preview.status")}
        </span>
      </div>

      <div className="grid grid-cols-[auto_1fr] gap-3 p-3">
        <div className="hidden w-24 flex-col gap-1 sm:flex">
          {rails.map((name, i) => (
            <span
              key={name}
              className={cn("rounded-full px-3 py-1.5 text-xs", i === 0 ? "bg-ink text-bg" : "text-n700")}
            >
              {name}
            </span>
          ))}
        </div>

        <div className="col-span-2 flex min-w-0 flex-col gap-3 sm:col-span-1">
          <div className="rounded-xl border border-hair bg-bg px-4 py-3.5">
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-2xs text-n600">{t("preview.kicker")}</p>
                <p className="mt-0.5 text-base font-semibold leading-snug">{t("preview.panelTitle")}</p>
              </div>
              <span className="flex-none text-2xs text-n600">{t("preview.time")}</span>
            </div>
            <div className="mt-3 flex flex-col divide-y divide-hair">
              {tasks.map((task, i) => (
                <div key={task.title} className="flex gap-2.5 py-2.5">
                  <span
                    className={cn(
                      "mt-1.5 size-2 flex-none rounded-full",
                      i === 0 ? "bg-s600" : "border border-n400",
                    )}
                  />
                  <div className="min-w-0">
                    <p className="text-sm leading-snug">{task.title}</p>
                    <p className="mt-0.5 text-xs text-n600">{task.body}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="flex items-end gap-4 rounded-xl border border-hair bg-bg px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <p className="text-2xs text-n600">{t("preview.signalKicker")}</p>
              <p className="mt-0.5 text-2xl font-semibold text-s700">{t("preview.signalValue")}</p>
              <p className="text-xs text-n600">{t("preview.signalBody")}</p>
            </div>
            <div className="flex h-12 flex-none items-end gap-1">
              {BARS.map((h, i) => (
                <span
                  key={i}
                  className={cn("w-2 rounded-t-sm", i === BARS.length - 1 ? "bg-s600" : "bg-s300")}
                  style={{ height: `${h}%` }}
                />
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

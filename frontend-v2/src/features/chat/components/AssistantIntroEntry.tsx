// Above the composer of a personal-assistant conversation that has started: the way back into the
// first meeting while something is still undecided. Once, after the person went straight to work
// instead, a one-line reminder; otherwise a quiet "让我更懂你". Settings' "重新认识一下" lands here
// too (`?intro=all`), going through every question with today's values as the defaults.
import { useEffect, useState } from "react"
import { Sparkles } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useSearchParams } from "react-router"
import { introSteps, type IntroMode } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { Dialog } from "@/shared/ui/Dialog"
import { AssistantIntro } from "./AssistantIntro"

export const INTRO_PARAM = "intro"

export function AssistantIntroEntry({
  quiet,
  onPick,
}: {
  /** The conversation has turns and nothing is running or waiting on the person. */
  quiet: boolean
  onPick: (prompt: string) => void
}) {
  const { t } = useTranslation("chat")
  const meta = useAppearanceStore((s) => s.assistantMeta)
  const recordIntro = useAppearanceStore((s) => s.recordIntro)
  const [params, setParams] = useSearchParams()
  const [opened, setOpened] = useState<IntroMode | null>(null)
  const [reminding, setReminding] = useState(false)
  const [nudged, setNudged] = useState(false)
  const requested: IntroMode | null = params.get(INTRO_PARAM) === "all" ? "all" : null
  const mode = opened ?? requested
  const undecided = meta ? introSteps(meta, "undecided").length : 0
  const remind = quiet && meta?.intro.status === "bypassed" && !meta.intro.nudged && undecided > 0

  // The reminder is shown once: recorded the moment it appears, and kept on screen until answered.
  if (remind && !nudged) {
    setNudged(true)
    setReminding(true)
  }
  useEffect(() => {
    if (nudged) void recordIntro({ event: "nudged" }).catch(() => undefined)
  }, [nudged, recordIntro])

  const close = () => {
    setOpened(null)
    if (requested) {
      const next = new URLSearchParams(params)
      next.delete(INTRO_PARAM)
      setParams(next, { replace: true })
    }
  }

  return (
    <>
      {reminding ? (
        <div
          role="status"
          className="border-hair bg-card mx-auto mb-2 flex w-full max-w-190 flex-wrap items-center gap-x-3 gap-y-1.5 rounded-2xl border px-4 py-2.5 text-sm"
        >
          <span className="text-ink min-w-0 flex-1">{t("assistant.intro.nudge")}</span>
          <button
            type="button"
            className="bg-ink text-bg h-8 rounded-full px-3.5 text-sm font-medium"
            onClick={() => {
              setReminding(false)
              setOpened("undecided")
            }}
          >
            {t("assistant.intro.nudgeYes")}
          </button>
          <button
            type="button"
            className="text-n700 hover:bg-hairsoft h-8 rounded-full px-3 text-sm"
            onClick={() => {
              setReminding(false)
              void recordIntro({ event: "dismiss" }).catch(() => undefined)
            }}
          >
            {t("assistant.intro.nudgeNo")}
          </button>
        </div>
      ) : (
        quiet &&
        undecided > 0 && (
          <div className="mx-auto mb-1.5 flex w-full max-w-190 px-1">
            <button
              type="button"
              onClick={() => setOpened("undecided")}
              className="text-n600 hover:text-ink inline-flex items-center gap-1.5 text-xs"
            >
              <Sparkles size={13} strokeWidth={2} aria-hidden />
              {t("assistant.intro.entry")}
            </button>
          </div>
        )
      )}
      {mode && meta && (
        <Dialog open onClose={close} label={t("assistant.intro.dialogTitle")}>
          <AssistantIntro key={mode} mode={mode} onPick={onPick} onClose={close} framed={false} />
        </Dialog>
      )}
    </>
  )
}

// The four-step tour of how the person and the assistant work together (VOICE_SEAM_PLAN §12.6):
// you only talk to me; I open a task card and send a helper; tap the card to see the work; I report
// when it is done. Shown once by itself (recorded on the profile, so no other app shows it again),
// and any time from the topbar.
import { useState } from "react"
import { BellRing, ClipboardList, DoorOpen, MessageCircle, type LucideIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { introStartsByItself } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { cn } from "@/shared/lib/cn"
import { Dialog } from "@/shared/ui/Dialog"
import { useAssistantTour } from "../stores/assistant-tour"

const STEPS: { key: string; Icon: LucideIcon }[] = [
  { key: "talk", Icon: MessageCircle },
  { key: "card", Icon: ClipboardList },
  { key: "site", Icon: DoorOpen },
  { key: "report", Icon: BellRing },
]

/** Mounted once on the assistant page: opens the tour by itself the first time, and whenever asked. */
export function AssistantTourHost() {
  const open = useAssistantTour((s) => s.open)
  const setOpen = useAssistantTour((s) => s.setOpen)
  const meta = useAppearanceStore((s) => s.assistantMeta)
  const recordIntro = useAppearanceStore((s) => s.recordIntro)
  const [offered, setOffered] = useState(false)
  // Once per visit, after the profile is known, and never on top of the first meeting's questions.
  if (!offered && meta && !meta.intro.tourSeen && !introStartsByItself(meta)) {
    setOffered(true)
    setOpen(true)
  }
  const close = () => {
    setOpen(false)
    if (meta && !meta.intro.tourSeen) void recordIntro({ event: "tour" }).catch(() => undefined)
  }
  return open ? <AssistantTour onClose={close} /> : null
}

export function AssistantTour({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation("chat")
  const [index, setIndex] = useState(0)
  const step = STEPS[index]
  const last = index === STEPS.length - 1
  return (
    <Dialog open onClose={onClose} label={t("assistant.tour.title")}>
      <div data-testid="assistant-tour" className="flex flex-col items-center text-center">
        <p className="text-n600 text-xs">{t("assistant.tour.title")} · {t("assistant.tour.position", { index: index + 1, count: STEPS.length })}</p>
        <div className="bg-a100 text-a700 mt-4 flex size-16 items-center justify-center rounded-full">
          <step.Icon size={30} strokeWidth={1.8} aria-hidden />
        </div>
        <h2 className="text-ink mt-4 text-lg font-medium">{t(`assistant.tour.steps.${step.key}.title`)}</h2>
        <p className="text-n800 mt-2 max-w-72 text-sm leading-relaxed">{t(`assistant.tour.steps.${step.key}.body`)}</p>
        <div className="mt-4 flex gap-1.5" aria-hidden>
          {STEPS.map((item, i) => <span key={item.key} className={cn("size-1.5 rounded-full", i === index ? "bg-ink" : "bg-n300")} />)}
        </div>
        <div className="mt-5 flex w-full items-center justify-between">
          <button type="button" onClick={onClose} className="text-n600 hover:text-ink text-sm">{t("assistant.tour.skip")}</button>
          <button type="button" onClick={() => (last ? onClose() : setIndex(index + 1))}
            className="bg-ink text-bg h-9 rounded-full px-5 text-sm font-medium">
            {t(last ? "assistant.tour.done" : "assistant.tour.next")}
          </button>
        </div>
      </div>
    </Dialog>
  )
}

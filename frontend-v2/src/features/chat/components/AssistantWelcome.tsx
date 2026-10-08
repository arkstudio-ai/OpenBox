// The first thing someone sees in the personal assistant: who it is, what to
// hand it, and a few things to try. A card fills the composer rather than
// sending, so a first-time user sees the words before anything happens.
import { useState } from "react"
import { BellRing, Brain, ClipboardList, Coins, ListChecks, Sun } from "lucide-react"
import type { LucideIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { introStartsByItself } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { AssistantAvatar } from "./AssistantAvatar"
import { AssistantIntro } from "./AssistantIntro"

const IDEAS: Array<{ key: string; icon: LucideIcon }> = [
  { key: "delegate", icon: ClipboardList },
  { key: "progress", icon: ListChecks },
  { key: "waiting", icon: BellRing },
  { key: "remember", icon: Brain },
  { key: "briefing", icon: Sun },
  { key: "credits", icon: Coins },
]

function timeOfDay(): "morning" | "afternoon" | "evening" {
  const hour = new Date().getHours()
  // Before dawn is still the evening for anyone awake to read this.
  if (hour >= 5 && hour < 12) return "morning"
  if (hour >= 12 && hour < 18) return "afternoon"
  return "evening"
}

export function AssistantWelcome({ onPick }: { onPick: (prompt: string) => void }) {
  const { t } = useTranslation("chat")
  // What the person asked to be called (Settings, or told to the assistant); a sign-in name such as
  // "memoryqa_2026…" is not a way to greet anyone, so without one the greeting has no name.
  const { name: assistantName, address } = useAppearanceStore((state) => state.assistant)
  const meta = useAppearanceStore((state) => state.assistantMeta)
  // Whether the first meeting opens is decided once, when the person's settings are known, so it
  // neither flashes on a guess nor vanishes when its last answer completes it.
  const [meeting, setMeeting] = useState<boolean | null>(null)
  if (meeting === null && meta) setMeeting(introStartsByItself(meta))
  const when = timeOfDay()
  return (
    <div className="scr min-h-0 flex-1 overflow-y-auto px-4 pt-8 pb-4 sm:px-6.5">
      <div className="mx-auto flex w-full max-w-190 flex-col items-center text-center">
        <AssistantAvatar size="lg" />
        <h1 className="mt-5 text-2xl font-medium tracking-tight sm:text-3xl">
          {address
            ? t(`assistant.welcome.greeting.${when}`, { name: address })
            : t(`assistant.welcome.greetingPlain.${when}`)}
        </h1>
        <p className="text-n700 mt-3 max-w-130 text-base leading-relaxed">
          {assistantName
            ? t("assistant.welcome.introNamed", { name: assistantName })
            : t("assistant.welcome.intro")}
        </p>
        {meeting ? (
          <div className="mt-7 flex w-full justify-center">
            <AssistantIntro mode="auto" onPick={onPick} onClose={() => setMeeting(false)} />
          </div>
        ) : (
          <div className="mt-7 grid w-full grid-cols-1 gap-2.5 text-start sm:grid-cols-2">
            {IDEAS.map(({ key, icon: Icon }) => (
              <button
                key={key}
                type="button"
                onClick={() => onPick(t(`assistant.welcome.ideas.${key}.prompt`))}
                className="border-hair bg-card hover:border-n400 group flex items-start gap-3 rounded-2xl border p-4 text-start transition-colors"
              >
                <span className="bg-hairsoft text-n800 group-hover:bg-a200 flex size-9 flex-none items-center justify-center rounded-xl transition-colors">
                  <Icon size={17} strokeWidth={2} />
                </span>
                <span className="min-w-0">
                  <span className="text-ink block text-base font-medium">
                    {t(`assistant.welcome.ideas.${key}.title`)}
                  </span>
                  <span className="text-n600 mt-0.5 block text-sm leading-snug">
                    {t(`assistant.welcome.ideas.${key}.prompt`)}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
        <p className="text-n600 mt-6 text-sm">{t("assistant.welcome.hint")}</p>
      </div>
    </div>
  )
}

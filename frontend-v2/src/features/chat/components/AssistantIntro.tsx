// The first meeting with the personal assistant (backend assistant/profile.py `intro_event`): at most
// four short questions, one at a time, each answered with a tap or a few words and each skippable,
// then one real thing to start with. The app asks, not the model, so nothing is asked twice,
// restated or lost; every answer is saved at once as the person's decision.
import { useState, type FormEvent, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import {
  ASSISTANT_LENGTHS,
  BUSINESSES,
  introSteps,
  type IntroMode,
  type IntroStep,
} from "@/shared/appearance/assistant-profile"
import { useAppearanceStore, type IntroEvent } from "@/shared/appearance/store"
import { cn } from "@/shared/lib/cn"
import { toast } from "@/shared/ui/Toast"

interface Suggestion {
  title: string
  prompt: string
}

const choice = "bg-card text-ink h-9 rounded-full border px-3.5 text-sm transition-colors disabled:opacity-50"
const link = "text-n600 hover:text-ink text-sm underline-offset-2 hover:underline disabled:opacity-50"

export function AssistantIntro({
  mode,
  onPick,
  onClose,
  framed = true,
}: {
  mode: IntroMode
  /** Its own card on the welcome page; bare inside a dialog, which is the card there. */
  framed?: boolean
  /** Puts a first task's prompt into the composer; it is not sent. */
  onPick: (prompt: string) => void
  /** The meeting is over here: finished, put off, or closed. */
  onClose: () => void
}) {
  const { t } = useTranslation("chat")
  const meta = useAppearanceStore((s) => s.assistantMeta)
  const profile = useAppearanceStore((s) => s.assistant)
  const recordIntro = useAppearanceStore((s) => s.recordIntro)
  // Fixed when the meeting opens, so an answer never reshuffles what is left.
  const [steps] = useState<IntroStep[]>(() => (meta ? introSteps(meta, mode) : []))
  const [handled, setHandled] = useState<IntroStep[]>([])
  const [busy, setBusy] = useState(false)
  // A question decided elsewhere meanwhile (the phone, Settings, the assistant in chat) is not asked here.
  const current = steps.find((step) => !handled.includes(step) && (mode === "all" || !meta?.decided[step]))

  const send = async (event: IntroEvent, step: IntroStep) => {
    setBusy(true)
    try {
      await recordIntro(event)
      setHandled((list) => [...list, step])
    } catch {
      toast("error", t("assistant.intro.failed"))
    } finally {
      setBusy(false)
    }
  }
  const answer = (step: IntroStep, value: string) => void send({ event: "answer", step, value }, step)
  const later = () => {
    // Only the meeting that came by itself is put off; one the person opened just closes.
    if (mode === "auto") void recordIntro({ event: "dismiss" }).catch(() => undefined)
    onClose()
  }

  if (!current) {
    const kind = (BUSINESSES as readonly string[]).includes(profile.business) ? profile.business : "general"
    const ideas = t(`assistant.intro.suggest.${kind}`, { returnObjects: true }) as unknown as Suggestion[]
    return (
      <Card framed={framed}>
        <p className="text-ink text-md font-medium">
          {profile.address
            ? t("assistant.intro.doneTitle", { name: profile.address })
            : t("assistant.intro.doneTitlePlain")}
        </p>
        <div className="mt-3 flex flex-col gap-2">
          {Array.isArray(ideas) &&
            ideas.map((idea) => (
              <button
                key={idea.title}
                type="button"
                onClick={() => {
                  onPick(idea.prompt)
                  onClose()
                }}
                className="border-hair hover:border-n400 text-ink rounded-xl border px-4 py-2.5 text-start text-sm transition-colors"
              >
                {idea.title}
              </button>
            ))}
        </div>
        <div className="mt-3">
          <button type="button" className={link} onClick={onClose}>
            {t("assistant.intro.doneLater")}
          </button>
        </div>
      </Card>
    )
  }

  const position = steps.indexOf(current) + 1
  return (
    <Card framed={framed}>
      {mode === "auto" && position === 1 && (
        <p className="text-n700 mb-3 text-sm">{t("assistant.intro.lead")}</p>
      )}
      <div className="flex items-baseline justify-between gap-3">
        <p id={`intro-${current}`} className="text-ink text-md font-medium">
          {t(`assistant.intro.${current}.question`)}
        </p>
        <span className="text-n500 flex-none text-xs tabular-nums">
          {t("assistant.intro.progress", { current: position, total: steps.length })}
        </span>
      </div>
      <div role="group" aria-labelledby={`intro-${current}`} className="mt-3">
        <Question
          key={current}
          step={current}
          mode={mode}
          busy={busy}
          onAnswer={(value) => answer(current, value)}
        />
      </div>
      <div className="mt-3 flex items-center gap-4">
        <button
          type="button"
          className={link}
          disabled={busy}
          onClick={() => void send({ event: "skip", step: current }, current)}
        >
          {t("assistant.intro.skip")}
        </button>
        {mode === "auto" ? (
          <button type="button" className={link} disabled={busy} onClick={later}>
            {t("assistant.intro.later")}
          </button>
        ) : (
          <button type="button" className={link} disabled={busy} onClick={onClose}>
            {t("assistant.intro.close")}
          </button>
        )}
      </div>
    </Card>
  )
}

function Card({ children, framed }: { children: ReactNode; framed: boolean }) {
  const { t } = useTranslation("chat")
  return (
    <section
      aria-label={t("assistant.intro.dialogTitle")}
      className={cn("w-full text-start", framed && "border-hair bg-card max-w-130 rounded-2xl border p-5")}
      data-testid="assistant-intro"
    >
      {children}
    </section>
  )
}

/** One question: its quick choices and, where it takes words, a short field. In "all" mode today's
 *  value is the one shown as chosen, so going through again is a matter of confirming. */
function Question({
  step,
  mode,
  busy,
  onAnswer,
}: {
  step: IntroStep
  mode: IntroMode
  busy: boolean
  onAnswer: (value: string) => void
}) {
  const { t } = useTranslation("chat")
  const profile = useAppearanceStore((s) => s.assistant)
  const username = useAuthStore((s) => s.user?.username ?? "")
  const current = mode === "all" ? profile[step] : ""
  const [other, setOther] = useState(
    step === "business" && current && !(BUSINESSES as readonly string[]).includes(current),
  )
  const options: { value: string; label: string }[] =
    step === "address"
      ? [
          // The sign-in name is only offered, never assumed.
          ...(username
            ? [{ value: username, label: t("assistant.intro.address.useAccount", { name: username }) }]
            : []),
          { value: "", label: t("assistant.intro.address.none") },
        ]
      : step === "name"
        ? [{ value: "", label: t("assistant.intro.name.keep") }]
        : step === "length"
          ? ASSISTANT_LENGTHS.map((value) => ({ value, label: t(`assistant.intro.length.${value}`) }))
          : BUSINESSES.map((value) => ({ value, label: t(`assistant.intro.business.${value}`) }))
  const takesWords = step === "address" || step === "name" || (step === "business" && other)
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-wrap gap-2">
        {options.map((option) => (
          <button
            key={option.label}
            type="button"
            disabled={busy}
            aria-pressed={
              mode === "all" && step !== "address" && step !== "name" ? current === option.value : undefined
            }
            onClick={() => onAnswer(option.value)}
            className={cn(
              choice,
              mode === "all" && current === option.value && step !== "address" && step !== "name"
                ? "border-ink"
                : "border-hair hover:border-n400",
            )}
          >
            {option.label}
          </button>
        ))}
        {step === "business" && !other && (
          <button
            type="button"
            disabled={busy}
            onClick={() => setOther(true)}
            className={cn(choice, "border-hair hover:border-n400")}
          >
            {t("assistant.intro.business.other")}
          </button>
        )}
      </div>
      {takesWords && (
        <Words
          step={step}
          initial={step === "business" ? (other && current ? current : "") : current}
          busy={busy}
          onAnswer={onAnswer}
        />
      )}
    </div>
  )
}

function Words({
  step,
  initial,
  busy,
  onAnswer,
}: {
  step: IntroStep
  initial: string
  busy: boolean
  onAnswer: (value: string) => void
}) {
  const { t } = useTranslation("chat")
  const [text, setText] = useState(initial)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (text.trim() && !busy) onAnswer(text.trim())
  }
  return (
    <form onSubmit={submit} className="flex max-w-sm items-center gap-2">
      <input
        type="text"
        value={text}
        maxLength={20}
        onChange={(event) => setText(event.target.value)}
        placeholder={t(`assistant.intro.${step}.placeholder`)}
        aria-label={t(`assistant.intro.${step}.question`)}
        className="border-hair bg-card text-ink placeholder:text-n600 focus:border-n400 h-9 min-w-0 flex-1 rounded-full border px-3.5 text-sm outline-none"
      />
      <button
        type="submit"
        disabled={busy || !text.trim()}
        className="bg-ink text-bg h-9 flex-none rounded-full px-4 text-sm font-medium disabled:opacity-40"
      >
        {t("assistant.intro.confirm")}
      </button>
    </form>
  )
}

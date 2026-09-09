// The agent's clarifying questions, asked at the end of the transcript.
//
// This is a turn-taking moment, and it reads as one: the card sits where the
// next message would, and scrolls with the conversation like everything else.
//
// It used to hang below the scroll area, pinned above the composer, so a
// waiting run could never be scrolled away from. That cost more than it was
// worth — as a sibling of a `flex-1 min-h-0` message list, a tall card (a
// segment approval carries three scripts and their prompts) squeezed the list
// to nothing and the conversation stopped scrolling entirely. In the flow, a
// card of any height costs only its own scrolling.
//
// Once answered it disappears — the exchange lives on in the conversation as
// the question tool's own row, so nothing is lost by dismissing it here.
import { useEffect, useRef } from "react"
import { useTranslation } from "react-i18next"
import { Check, ChevronLeft, ChevronRight } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { QuestionDraftAnswer, QuestionItem, QuestionRequest } from "@/shared/types/api"
import { useRejectQuestion, useReplyQuestion } from "../api/question"
import { VideoApprovalDetail } from "./VideoApprovalDetail"
import { questionAnswers, useQuestionDraft } from "../hooks/useQuestionDraft"
import { useQuestionPager } from "../hooks/useQuestionPager"

interface OneProps {
  item: QuestionItem
  index: number
  total: number
  draft: QuestionDraftAnswer
  disabled: boolean
  onChange: (draft: QuestionDraftAnswer) => void
  onComplete: () => void
}

function OneQuestion({ item, index, total, draft, disabled, onChange, onComplete }: OneProps) {
  const { t } = useTranslation("chat")
  const picked = draft.use_custom ? [] : draft.selected
  const options = item.options ?? []
  const multiple = item.multiple ?? false
  // Absent means allowed — only an explicit false closes it.
  const allowCustom = item.custom !== false

  const toggle = (label: string) => {
    const selected = !multiple
      ? [label]
      : picked.includes(label)
        ? picked.filter((v) => v !== label)
        : [...picked, label]
    onChange({ ...draft, selected, use_custom: false })
    if (!multiple) onComplete()
  }

  return (
    <fieldset disabled={disabled} className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-col gap-0.5">
        <span className="text-n600 flex items-center gap-1.5 text-xs">
          {item.header || t("question.title")}
        </span>
        <span className="text-ink text-base">{item.question}</span>
      </div>

      <VideoApprovalDetail item={item} />

      {options.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {options.map((o) => {
            const on = picked.includes(o.label)
            return (
              <button
                key={o.label}
                type="button"
                onClick={() => toggle(o.label)}
                aria-pressed={on}
                title={o.description}
                className={cn(
                  "flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors",
                  on ? "border-accent bg-a100 text-a800" : "border-hair text-ink hover:bg-hairsoft",
                )}
              >
                {on && <Check className="size-3" strokeWidth={3} />}
                {o.label}
              </button>
            )
          })}
        </div>
      )}

      {/* Offered unless the asker closed it. The agent is told not to add a
          catch-all option because this is here, so its questions always keep
          it; only the system's own — plan mode's Yes/No — turn it off, where
          a text box would invite an answer nothing reads. */}
      {allowCustom && (
        <input
          value={draft.custom}
          maxLength={5000}
          onChange={(e) => {
            onChange({ ...draft, custom: e.target.value, use_custom: true })
          }}
          onFocus={() => {
            if (draft.custom) onChange({ ...draft, use_custom: true })
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.nativeEvent.isComposing || event.keyCode === 229) return
            event.preventDefault()
            event.stopPropagation()
            if (draft.custom.trim()) onComplete()
          }}
          enterKeyHint={index < total - 1 ? "next" : "done"}
          placeholder={options.length > 0 ? t("question.other") : t("question.answer")}
          aria-label={item.question}
          className="border-hair bg-bg text-ink placeholder:text-n500 focus:border-accent w-full rounded-lg border px-3 py-1.5 text-sm outline-none"
        />
      )}
      {index < total - 1 && (multiple || draft.use_custom) && (
        <p className="text-n600 text-xs">
          {t(multiple ? "question.multipleNextHint" : "question.customNextHint")}
        </p>
      )}
    </fieldset>
  )
}

export function QuestionDock({ request }: { request: QuestionRequest }) {
  const { t } = useTranslation("chat")
  const reply = useReplyQuestion()
  const reject = useRejectQuestion()
  const questions = request.questions ?? []
  const busy = reply.isPending || reject.isPending
  const { draft, update, saving, saveError, retrySave } = useQuestionDraft(request, busy)
  const answers = questionAnswers(draft)
  const answered = answers.filter((answer) => answer.length > 0).length
  const complete = answered === questions.length
  const { page, goTo } = useQuestionPager(request.id, answers)
  const heading = useRef<HTMLDivElement>(null)
  const previousPage = useRef(page)
  useEffect(() => {
    if (previousPage.current !== page) heading.current?.focus({ preventScroll: true })
    previousPage.current = page
  }, [page])

  if (questions.length === 0) return null

  return (
    <div className="border-hair bg-card mx-auto mb-2 w-full max-w-190 rounded-xl border p-4 shadow-sm">
      <nav aria-label={t("question.navigation")} className="mb-3 flex items-center justify-between gap-2">
        {questions.length > 1 && (
          <button
            type="button"
            disabled={busy || page === 0}
            onClick={() => goTo(page - 1)}
            className="text-n600 hover:bg-hairsoft flex min-h-9 items-center gap-1 rounded-full px-2 text-xs disabled:opacity-40"
          >
            <ChevronLeft className="size-4" aria-hidden />
            {t("question.previous")}
          </button>
        )}
        <div
          ref={heading}
          tabIndex={-1}
          aria-live="polite"
          aria-atomic="true"
          aria-label={t("question.page", { current: page + 1, count: questions.length })}
          className="text-n600 text-xs tabular-nums outline-none"
        >
          {page + 1}/{questions.length}
        </div>
        {questions.length > 1 && (
          <button
            type="button"
            disabled={busy || page === questions.length - 1}
            onClick={() => goTo(page + 1)}
            className="text-n600 hover:bg-hairsoft flex min-h-9 items-center gap-1 rounded-full px-2 text-xs disabled:opacity-40"
          >
            {t("question.next")}
            <ChevronRight className="size-4" aria-hidden />
          </button>
        )}
      </nav>
      <div>
        <OneQuestion
          key={`${request.id}:${page}`}
          item={questions[page]}
          index={page}
          total={questions.length}
          draft={draft[page]}
          disabled={busy}
          onChange={(value) => update(page, value)}
          onComplete={() => {
            if (!busy && page < questions.length - 1) goTo(page + 1)
          }}
        />
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2.5">
        <button
          type="button"
          onClick={() => reply.mutate({ requestId: request.id, answers })}
          disabled={!complete || busy}
          className="bg-ink text-bg rounded-full px-4 py-1.5 text-sm disabled:opacity-40"
        >
          {reply.isPending ? t("question.submitting") : t("question.submit")}
        </button>
        <button
          type="button"
          onClick={() => reject.mutate(request.id)}
          disabled={busy}
          className="border-hair text-ink hover:bg-hairsoft rounded-full border px-4 py-1.5 text-sm disabled:opacity-60"
        >
          {reject.isPending
            ? t("question.submitting")
            : t(questions.length > 1 ? "question.skipAll" : "question.skip")}
        </button>
        {!complete && (
          <span className="text-n600 text-xs">
            {t("question.progress", { answered, count: questions.length })}
          </span>
        )}
      </div>
      {saving && (
        <p className="text-n600 mt-2 text-xs" role="status">
          {t("question.saving")}
        </p>
      )}
      {saveError && (
        <p className="text-danger mt-2 text-xs" role="alert">
          {t(saveError === "conflict" ? "question.draftConflict" : "question.draftFailed")}
          <button type="button" className="ml-2 underline" onClick={retrySave}>
            {t("question.retrySave")}
          </button>
        </p>
      )}
      {(reply.isError || reject.isError) && (
        <p className="text-danger mt-2 text-xs" role="alert">
          {t("question.submitFailed")}
        </p>
      )}
    </div>
  )
}

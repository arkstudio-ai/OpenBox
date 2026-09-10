// What was asked, and what the user chose — shown in the conversation after
// the dock above the composer has gone.
//
// The dock is for answering; this is the record. Without it the exchange left
// only "Asked 2 questions" in the tool chain, so scrolling back told you a
// decision had been made but not which way.
import { useTranslation } from "react-i18next"
import type { ToolPart } from "@/shared/types/api"
import { readTakeoverDetail, takeoverReasonKey } from "../DesktopTakeoverDetail"

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []
}

/** Pair each question with the labels chosen for it. */
export function questionPairs(part: ToolPart): Array<{ question: string; answer: string[] }> {
  const questions = strings(part.metadata?.questions)
  const answers = Array.isArray(part.metadata?.answers) ? part.metadata.answers : []
  return questions.map((question, i) => ({ question, answer: strings(answers[i]) }))
}

export function QuestionAnswered({ part }: { part: ToolPart }) {
  const { t } = useTranslation("chat")
  const pairs = questionPairs(part)
  const state = part.metadata?.question_status
  const stateLabel = state === "superseded" ? t("question.superseded")
    : state === "cancelled" ? t("question.cancelled")
    : state === "expired" ? t("question.expired")
    : part.status === "waiting_input" ? t("question.waiting") : null
  if (pairs.length === 0 && !stateLabel) return null
  // desktop_takeover leaves what blocked the agent and where, so the record
  // reads "the user solved a slider on host X" rather than a bare question.
  const takeover = readTakeoverDetail(part.metadata?.takeover)

  return (
    <div className="flex flex-col gap-2">
      {stateLabel && <span className="text-n600 text-xs">{stateLabel}</span>}
      {takeover && (
        <span className="text-a800 text-xs">
          {t("takeover.record", {
            reason: t(takeoverReasonKey(takeover.reason)),
            host: takeover.host || takeover.url,
          })}
        </span>
      )}
      {pairs.map(({ question, answer }) => (
        <div key={question} className="flex flex-col gap-0.5">
          <span className="text-n600 text-xs">{question}</span>
          <span className="text-ink text-sm">
            {answer.length > 0 ? answer.join("、") : t("question.unanswered")}
          </span>
        </div>
      ))}
    </div>
  )
}

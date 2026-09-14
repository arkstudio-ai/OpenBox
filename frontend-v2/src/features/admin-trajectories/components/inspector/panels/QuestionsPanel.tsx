import { useTranslation } from "react-i18next"
import { Check } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { fieldState } from "../../../utils/availability"
import { isPlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { JsonTree } from "../JsonTree"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

interface QuestionItemProps {
  question: unknown
  answer: readonly string[] | null
  index: number
}

function optionLabel(option: unknown): string {
  if (typeof option === "string") return option
  if (isPlainObject(option) && typeof option.label === "string") return option.label
  return JSON.stringify(option)
}

function QuestionItem({ question, answer, index }: QuestionItemProps) {
  const { t } = useTranslation(NS)
  if (!isPlainObject(question)) return <JsonTree value={question} />
  const options = Array.isArray(question.options) ? question.options : []
  const labels = options.map(optionLabel)
  const multiple = question.multiple === true || question.multiSelect === true
  const custom = answer?.filter((value) => !labels.includes(value)) ?? []
  return (
    <li className="border-hair flex flex-col gap-1.5 rounded-lg border p-3">
      <p className="text-n600 text-2xs">
        {t("question.number", { index: index + 1 })} · {t(multiple ? "question.multiple" : "question.single")}
        {typeof question.header === "string" && ` · ${question.header}`}
      </p>
      <p className="text-ink text-sm whitespace-pre-wrap">
        {typeof question.question === "string" ? question.question : JSON.stringify(question.question)}
      </p>
      <ul className="flex flex-col gap-1">
        {options.map((option, optionIndex) => {
          const chosen = answer?.includes(labels[optionIndex]) ?? false
          return (
            <li
              key={optionIndex}
              className={cn(
                "flex items-start gap-2 rounded px-2 py-1 text-xs",
                chosen ? "bg-s100 text-s800" : "text-n700",
              )}
            >
              <span className="mt-0.5 size-3 flex-none">
                {chosen && <Check size={12} aria-label={t("question.chosen")} />}
              </span>
              <span>
                {labels[optionIndex]}
                {isPlainObject(option) && typeof option.description === "string" && (
                  <span className="text-n500 block">{option.description}</span>
                )}
              </span>
            </li>
          )
        })}
      </ul>
      {custom.length > 0 && (
        <p className="text-xs">{t("question.customAnswer", { value: custom.join(", ") })}</p>
      )}
      {isPlainObject(question.detail) && <JsonTree value={question.detail} openDepth={1} />}
    </li>
  )
}

/** The questions as asked, with the choices recorded by this position. Read-only: nothing can be answered here. */
export function QuestionsPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const questions = fieldState(record, "questions")
  const answers = Array.isArray(record.data?.answers) ? record.data.answers : null
  return (
    <div className="flex flex-col gap-3" data-testid="trajectory-questions">
      <p className="text-n600 text-xs">{t("question.readOnly")}</p>
      {questions.state === "available" && Array.isArray(questions.value) ? (
        questions.value.length ? (
          <ol className="flex flex-col gap-2">
            {questions.value.map((question, index) => {
              const answer =
                answers && Array.isArray(answers[index]) ? (answers[index] as unknown[]).map(String) : null
              return <QuestionItem key={index} question={question} answer={answer} index={index} />
            })}
          </ol>
        ) : (
          <AvailabilityNote state="empty" />
        )
      ) : (
        <ValueView field={questions} />
      )}
    </div>
  )
}

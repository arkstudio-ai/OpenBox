import { useContext } from "react"
import { useTranslation } from "react-i18next"
import type { AssistantResult } from "../api/assistant"
import { AssistantReadContext } from "../hooks/assistant-read-context"

interface Props { result: AssistantResult | null }

/** Each fact has its own authority: execution result, inbox, finalized report, visible-answer cursor. */
export function AssistantResultFacts({ result }: Props) {
  const { t } = useTranslation("chat")
  const context = useContext(AssistantReadContext)
  const answer = context?.snapshot?.answers.find((item) => item.message_id === result?.processed_message_id)
  const sequence = result?.processed_sequence ?? answer?.sequence
  const read = !!sequence && !!context?.snapshot && context.snapshot.last_seen_sequence >= sequence
  const outcome = !result ? t("assistant.executionPending") : result.outcome === "succeeded" ? t("assistant.executionSucceeded")
    : result.outcome === "aborted" ? t("assistant.executionStopped") : t("assistant.executionFailed")
  return <dl className="mt-3 grid grid-cols-2 gap-x-5 gap-y-2 text-xs">
    <div><dt className="text-n600">{t("assistant.execution")}</dt><dd>{outcome}</dd></div>
    <div><dt className="text-n600">{t("assistant.acceptance")}</dt><dd>{result?.assistant_inbox_id ? t("assistant.accepted") : t("assistant.pending")}</dd></div>
    <div><dt className="text-n600">{t("assistant.processing")}</dt><dd>{result?.delivery_state === "processed" ? t("assistant.processed")
      : result?.delivery_state === "blocked" ? t("assistant.reportBlocked") : t("assistant.pending")}</dd></div>
    <div><dt className="text-n600">{t("assistant.reading")}</dt><dd>{read ? t("assistant.read") : sequence ? t("assistant.unread") : t("assistant.noAnswer")}</dd></div>
  </dl>
}

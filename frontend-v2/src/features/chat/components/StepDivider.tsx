import { useTranslation } from "react-i18next"
import type { AgentSwitchPart, CompactionPart, RetryPart } from "@/shared/types/api"

/** Thin centered rule for compaction / retry / agent-switch markers. */
export function StepDivider({ part }: { part: CompactionPart | RetryPart | AgentSwitchPart }) {
  const { t } = useTranslation("chat")
  const text =
    part.type === "compaction"
      ? t("compaction")
      : part.type === "retry"
        ? t("retry", { attempt: part.attempt })
        : t("agentSwitch", { agent: part.agent })
  return <ConversationDivider label={text} />
}

export function ConversationDivider({ label }: { label: string }) {
  return (
    <div className="text-n500 flex items-center gap-3 py-1 text-xs">
      <span className="bg-hair h-px flex-1" />
      <span>{label}</span>
      <span className="bg-hair h-px flex-1" />
    </div>
  )
}

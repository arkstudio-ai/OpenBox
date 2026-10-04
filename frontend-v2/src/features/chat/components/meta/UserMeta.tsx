// Meta strip under a user bubble: right-aligned copy button + timestamp.
// User messages carry no reaction/fork semantics, so those actions are omitted.
import { Check, Copy } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useVerifiedAssistantCopy } from "../../hooks/useVerifiedAssistantCopy"
import { MessageTimestamp } from "./MetaBadges"
import { MetaContainer } from "./MetaContainer"
import { MetaIconButton } from "./MetaIconButton"

export function UserMeta({ sessionId, messageId, content, createdAt }: { sessionId: string; messageId: string; content: string; createdAt: string }) {
  const { t } = useTranslation("chat")
  const { copied, checking, copyReply } = useVerifiedAssistantCopy(sessionId, messageId, content, "text")
  return (
    <MetaContainer align="end">
      <MessageTimestamp iso={createdAt} />
      <MetaIconButton
        label={copied ? t("meta.copied") : t("meta.copyReply")}
        disabled={!content.trim() || checking}
        onClick={() => void copyReply()}
      >
        {copied ? <Check size={14} strokeWidth={1.8} /> : <Copy size={14} strokeWidth={1.8} />}
      </MetaIconButton>
    </MetaContainer>
  )
}

import { useEffect } from "react"
import { useTranslation } from "react-i18next"
import { useSendReceiptStore } from "../stores/send-receipts"
import { confirmPendingSend } from "../lib/pending-send"
import type { MessageWithParts } from "@/shared/types/api"

export function SendReceipt({ message }: { message: MessageWithParts }) {
  const { t } = useTranslation("chat")
  const clientId = message.client_message_id
  const optimistic = message.id.startsWith("tmp-")
  const state = useSendReceiptStore((store) => clientId ? store.receipts.get(`${message.session_id}:${clientId}`) : undefined)
  useEffect(() => {
    if (clientId && !optimistic) confirmPendingSend(clientId)
  }, [clientId, optimistic])
  if (!state) return null
  // A materialized server message also proves durable acceptance after a lost HTTP response.
  const effective = optimistic ? state : "accepted"
  return <span className="text-n600 text-xs" role="status">{effective === "sending" ? t("assistant.sendPending")
    : effective === "uncertain" ? t("assistant.sendUncertain") : t("assistant.sendAccepted")}</span>
}

// Meta strip under an assistant turn: data badges (model / tokens / latency)
// stacked over the action row (copy, react, fork) and the timestamp. In the
// personal assistant, a thumbs-down then asks why, so it can learn how to talk.
import { useState } from "react"
import { Check, Copy, GitFork, RefreshCw, ThumbsDown, ThumbsUp } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useCopy } from "@/shared/hooks/useCopy"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { toast } from "@/shared/ui/Toast"
import type { MessageReaction, ReactionReason, TokenUsage } from "@/shared/types/api"
import {
  REACTION_REASONS,
  useForkMessage,
  useRegenerate,
  useSetReaction,
  usePreserveAssistantEvidence,
} from "../../api/message-actions"
import { useModelChoiceStore } from "../../stores/model-choice"
import { useStreamStore } from "../../stores/stream"
import { LatencyBadge, MessageTimestamp, ModelBadge, TokenBadge } from "./MetaBadges"
import { MetaContainer } from "./MetaContainer"
import { MetaIconButton } from "./MetaIconButton"

interface Props {
  sessionId: string
  messageId: string
  content: string
  tokens?: TokenUsage | null
  reaction?: MessageReaction
  createdAt: string
  streaming: boolean
  durationSec: number
  completedDurationLabel?: string
  /** The personal assistant: actions and time only, no model / token / latency badges. */
  minimal?: boolean
}

export function AssistantMeta({
  sessionId,
  messageId,
  content,
  tokens,
  reaction,
  createdAt,
  streaming,
  durationSec,
  completedDurationLabel,
  minimal = false,
}: Props) {
  const { t } = useTranslation("chat")
  const preserveEvidence = usePreserveAssistantEvidence(sessionId)
  const { copied, copy } = useCopy()
  const navigate = useNavigate()
  const setReaction = useStreamStore((s) => s.setMessageReaction)
  const { mutate: react } = useSetReaction(sessionId)
  const { mutate: fork, isPending: forking } = useForkMessage(sessionId)
  const { mutate: regenerate, isPending: regenerating } = useRegenerate(sessionId)
  const errorMessage = useApiErrorMessage()
  const pickedModel = useModelChoiceStore((s) => s.picked.get(sessionId))
  const current = reaction ?? null
  // Asked right after a thumbs-down in the personal assistant; answering is optional.
  const [asking, setAsking] = useState(false)
  const [reason, setReason] = useState<ReactionReason | null>(null)

  const toggleReaction = (next: Exclude<MessageReaction, null>) => {
    const value: MessageReaction = current === next ? null : next
    setReaction(sessionId, messageId, value) // optimistic
    react({ messageId, reaction: value }, { onError: () => setReaction(sessionId, messageId, current) })
    setAsking(minimal && value === "down")
    setReason(null)
  }

  const giveReason = (picked: ReactionReason) => {
    setReason(picked)
    react(
      { messageId, reaction: "down", reason: picked },
      {
        onSuccess: () => {
          setReaction(sessionId, messageId, "down", picked)
          toast("success", t("meta.reason.thanks"))
        },
        onError: () => {
          setReason(null)
          toast("error", t("meta.reason.failed"))
        },
      },
    )
  }

  const onFork = () => {
    fork(messageId, { onSuccess: (session) => navigate(paths.chat(session.id)) })
  }

  const onRegenerate = () => {
    regenerate({ messageId, model: pickedModel }, { onError: (e) => toast("error", errorMessage(e)) })
  }

  return (
    <>
      <MetaContainer align="start">
        <div className="flex max-w-full min-w-0 flex-col items-start gap-1.5">
          {!minimal && <div className="flex max-w-full min-w-0 flex-wrap items-center gap-1">
            <ModelBadge sessionId={sessionId} />
            {tokens ? <TokenBadge tokens={tokens} /> : null}
            <LatencyBadge createdAt={createdAt} streaming={streaming} durationSec={durationSec}
              completedLabel={completedDurationLabel} />
          </div>}
          <div className="flex max-w-full min-w-0 flex-wrap items-center gap-1">
            <MetaIconButton
              label={copied ? t("meta.copied") : t("meta.copyReply")}
              disabled={!content.trim()}
              onClick={() => copy(`${content.trimEnd()}\n\n${t("aigc.label")}`)}
            >
              {copied ? <Check size={14} strokeWidth={1.8} /> : <Copy size={14} strokeWidth={1.8} />}
            </MetaIconButton>
            <MetaIconButton
              label={t("meta.likeReply")}
              active={current === "up"}
              disabled={streaming}
              onClick={() => toggleReaction("up")}
            >
              <ThumbsUp size={14} strokeWidth={1.8} />
            </MetaIconButton>
            <MetaIconButton
              label={t("meta.dislikeReply")}
              active={current === "down"}
              disabled={streaming}
              onClick={() => toggleReaction("down")}
            >
              <ThumbsDown size={14} strokeWidth={1.8} />
            </MetaIconButton>
            {!preserveEvidence && <><MetaIconButton
              label={forking ? t("meta.forking") : t("meta.forkMessage")}
              disabled={streaming || forking}
              onClick={onFork}
            >
              <GitFork size={14} strokeWidth={1.8} />
            </MetaIconButton>
            <MetaIconButton
              label={regenerating ? t("meta.regenerating") : t("meta.regenerate")}
              disabled={streaming || regenerating}
              onClick={onRegenerate}
            >
              <RefreshCw size={14} strokeWidth={1.8} className={regenerating ? "animate-spin" : undefined} />
            </MetaIconButton>
            </>}
            <MessageTimestamp iso={createdAt} />
          </div>
        </div>
      </MetaContainer>
      {asking && current === "down" && <ReasonPicker picked={reason} onPick={giveReason} />}
    </>
  )
}

/** Below the hover strip, so it stays while the pointer moves to it. */
function ReasonPicker({ picked, onPick }: { picked: ReactionReason | null; onPick: (reason: ReactionReason) => void }) {
  const { t } = useTranslation("chat")
  return (
    <div role="group" aria-label={t("meta.reason.title")} className="mt-1.5 flex flex-wrap items-center gap-1.5">
      <span className="text-n600 me-0.5 text-xs">{t("meta.reason.title")}</span>
      {REACTION_REASONS.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={picked === option}
          onClick={() => picked !== option && onPick(option)}
          className={cn(
            "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
            picked === option ? "border-ink text-ink" : "border-hair text-n700 hover:bg-hairsoft",
          )}
        >
          {t(`meta.reason.${option}`)}
        </button>
      ))}
    </div>
  )
}

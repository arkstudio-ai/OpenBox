// One assistant response, laid out exactly like DEEIX-Chat's message-bot:
// no avatar and no bubble — the turn owns the full column width, with the
// three collapsed trace rows stacked on top (process → thinking → tool chain)
// and the answer prose flowing underneath. Artifacts (diff chips, plan cards,
// notices) trail the body the way attachments/alerts do in the reference.
import { lazy, Suspense, useMemo } from "react"
import type { MessagePart } from "@/shared/types/api"
import { buildTurnView, type AssistantTurnMeta } from "../lib/turn-view"
import { AssistantMeta } from "./meta/AssistantMeta"
import { InlineErrorCard } from "./meta/InlineErrorCard"
import { AttachmentGallery } from "./AttachmentGallery"
import { isGalleryMedia } from "../lib/media"
import { FileChip, PatchChip } from "./PatchChip"
import { PlanPartCard } from "./PlanPartCard"
import { ProcessTrace } from "./ProcessTrace"
import { StepDivider } from "./StepDivider"
import { ThinkingRow } from "./ThinkingRow"
import { ThinkingTrace } from "./ThinkingTrace"
import { TodoCard } from "./TodoCard"
import { ToolChainTrace } from "./ToolChainTrace"

const Markdown = lazy(() => import("./Markdown"))

interface Props {
  parts: MessagePart[]
  sessionId: string
  meta: AssistantTurnMeta
  /** This is the live turn. */
  streaming: boolean
  /** Set while a stalled run is retrying, so the wait can say which try. */
  retry?: { attempt: number; maxAttempts: number }
  /** Abort the run — offered by the task card while one is in flight. */
  onStop?: () => void
  /** This turn holds the conversation's newest task card, so its card is the
   *  one that may be edited. */
  todoEditable?: boolean
}


export function AssistantTurn({ parts, sessionId, meta, streaming, retry, onStop, todoEditable }: Props) {
  const view = useMemo(() => buildTurnView(parts), [parts])
  const hasContent = view.content.trim().length > 0
  // "Thinking" is the state of having nothing yet — not of having no prose
  // yet. Once reasoning or a tool call has arrived the turn is visibly
  // working, and each of those blocks carries its own live heading, so a
  // second "正在思考中" underneath is both redundant and wrong: it claims the
  // model has not responded when it plainly has.
  const hasActivity =
    hasContent || view.thinking.trim().length > 0 || view.tools.length > 0
  // Traces that opened themselves collapse once prose starts arriving.
  const autoCollapseReady = hasContent
  // Per-part activity flags (`thinkingStreaming` flips every time a tool part
  // lands after reasoning; `toolsStreaming` drops in the gap between two
  // calls). Feeding those raw into the trace rows made titles flicker between
  // "正在思考" and "思考完成". Hold each trace live for its whole phase —
  // until the turn starts answering — instead.
  const preAnswer = streaming && !hasContent
  const thinkingLive = preAnswer && (view.thinkingStreaming || view.thinking.length > 0)
  const toolsLive = streaming && (view.toolsStreaming || !hasContent)

  return (
    <div className="group/msg flex w-full min-w-0 flex-col">
      <ProcessTrace
        contextTokens={view.contextTokens}
        durationSec={view.durationSec}
        streaming={preAnswer}
        autoCollapseReady={autoCollapseReady}
      />
      <ThinkingTrace
        text={view.thinking}
        streaming={thinkingLive}
        autoCollapseReady={autoCollapseReady}
      />
      {view.todo ? (
        <TodoCard
          todo={view.todo}
          sessionId={sessionId}
          streaming={streaming}
          onStop={onStop}
          editable={todoEditable}
        />
      ) : null}
      {/* Whatever the card did not account for: on a todo turn that is the
          calls made outside any task, and on every other turn it is the
          whole chain, exactly as before. */}
      <ToolChainTrace
        tools={view.tools}
        streaming={toolsLive}
        autoCollapseReady={autoCollapseReady}
      />

      <div className="text-ink w-full max-w-none min-w-0 overflow-hidden text-lg leading-8 [overflow-wrap:anywhere]">
        {streaming && !hasActivity ? (
          <ThinkingRow attempt={retry?.attempt} maxAttempts={retry?.maxAttempts} />
        ) : hasContent ? (
          <Suspense fallback={<p className="whitespace-pre-wrap">{view.content}</p>}>
            <Markdown text={view.content} streaming={streaming} />
          </Suspense>
        ) : null}
      </div>

      {meta.error ? (
        <InlineErrorCard
          error={meta.error}
          sessionId={sessionId}
          messageId={meta.messageId}
          streaming={streaming}
        />
      ) : null}

      {(view.patches.length > 0 || view.files.length > 0 || view.plans.length > 0) && (
        <div className="mt-2 flex flex-col gap-2">
          {view.patches.map((p) => (
            <PatchChip key={p.id} part={p} sessionId={sessionId} />
          ))}
          <AttachmentGallery parts={view.files.filter(isGalleryMedia)} />
          {view.files.filter((p) => !isGalleryMedia(p)).map((p) => (
            <FileChip key={p.id} part={p} />
          ))}
          {view.plans.map((p) => (
            <PlanPartCard key={p.id} part={p} sessionId={sessionId} />
          ))}
        </div>
      )}

      {view.notices.map((p) => (
        <StepDivider key={p.id} part={p} />
      ))}

      <AssistantMeta
        sessionId={sessionId}
        messageId={meta.messageId}
        content={view.content}
        tokens={meta.tokens}
        reaction={meta.reaction}
        createdAt={meta.createdAt}
        streaming={streaming}
        durationSec={view.durationSec}
      />
    </div>
  )
}

/** Placeholder before the assistant's first part arrives. */
export function TypingRow({
  retry,
}: {
  retry?: { attempt: number; maxAttempts: number }
}) {
  return (
    <div className="flex w-full min-w-0 flex-col">
      <ThinkingRow attempt={retry?.attempt} maxAttempts={retry?.maxAttempts} />
    </div>
  )
}

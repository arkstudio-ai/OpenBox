// One assistant response, laid out exactly like DEEIX-Chat's message-bot:
// no avatar and no bubble — the turn owns the full column width, with the
// collapsed process / thinking / tool-chain rows stacked on top, then one
// reading column holding the work log and the final answer in order, then the
// semantically grouped artifacts. Tool-step prose stays in the work log rather
// than being concatenated into the answer, but the log is open rather than
// folded: it is the turn's only account of itself.
import { lazy, Suspense, useMemo } from "react"
import { useTranslation } from "react-i18next"
import type { MessageWithParts } from "@/shared/types/api"
import { buildAssistantContentView } from "../lib/content-view"
import { buildTurnView, type AssistantTurnMeta } from "../lib/turn-view"
import { AssistantMeta } from "./meta/AssistantMeta"
import { InlineErrorCard } from "./meta/InlineErrorCard"
import { PatchChip } from "./PatchChip"
import { PlanPartCard } from "./PlanPartCard"
import { ProcessTrace } from "./ProcessTrace"
import { ResultArtifacts } from "./ResultArtifacts"
import { SkillJobReceipts } from "./SkillJobReceipts"
import { StepDivider } from "./StepDivider"
import { ThinkingRow } from "./ThinkingRow"
import { ThinkingTrace } from "./ThinkingTrace"
import { TodoCard } from "./TodoCard"
import { ToolChainTrace } from "./ToolChainTrace"
import { WorkLogTrace } from "./WorkLogTrace"

const Markdown = lazy(() => import("./Markdown"))

interface Props {
  messages: MessageWithParts[]
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

type ContentView = ReturnType<typeof buildAssistantContentView>
type TurnView = ReturnType<typeof buildTurnView>

function hasTurnActivity(content: ContentView, view: TurnView): boolean {
  return (
    content.hasFinal ||
    content.progress.length > 0 ||
    content.workEvents.length > 0 ||
    content.resultGroups.length > 0 ||
    Boolean(content.verification) ||
    Boolean(view.todo) ||
    view.thinking.trim().length > 0 ||
    view.tools.length > 0
  )
}

function needsFinalLabel(content: ContentView, view: TurnView): boolean {
  return (
    content.progress.length > 0 ||
    content.workEvents.length > 0 ||
    view.tools.length > 0 ||
    Boolean(view.todo) ||
    view.thinking.trim().length > 0 ||
    content.resultGroups.length > 0
  )
}

export function AssistantTurn({ messages, sessionId, meta, streaming, retry, onStop, todoEditable }: Props) {
  const { t } = useTranslation("chat")
  const parts = useMemo(() => messages.flatMap((message) => message.parts), [messages])
  const view = useMemo(() => buildTurnView(parts), [parts])
  const content = useMemo(() => buildAssistantContentView(messages, streaming), [messages, streaming])
  // "Thinking" is the state of having nothing yet — not of having no prose
  // yet. Once reasoning or a tool call has arrived the turn is visibly
  // working, and each of those blocks carries its own live heading, so a
  // second "正在思考中" underneath is both redundant and wrong: it claims the
  // model has not responded when it plainly has.
  const hasActivity = hasTurnActivity(content, view)
  // Per-part activity flags (`thinkingStreaming` flips every time a tool part
  // lands after reasoning; `toolsStreaming` drops in the gap between two
  // calls). Feeding those raw into the trace rows made titles flicker between
  // "正在思考" and "思考完成". Hold each trace live for its whole phase —
  // until the turn starts answering — instead.
  const preAnswer = streaming && !content.hasFinal
  const thinkingLive = preAnswer && (view.thinkingStreaming || view.thinking.length > 0)
  const toolsLive = streaming && (view.toolsStreaming || !content.hasFinal)
  const showFinalLabel = needsFinalLabel(content, view)

  return (
    <div className="group/msg flex w-full min-w-0 flex-col">
      <ProcessTrace
        contextTokens={view.contextTokens}
        durationSec={view.durationSec}
        streaming={preAnswer}
      />
      <ThinkingTrace text={view.thinking} streaming={thinkingLive} />
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
      <ToolChainTrace tools={view.tools} streaming={toolsLive} />
      <SkillJobReceipts parts={parts} />

      {/* The work log and the answer share one column and read in order: the
          narration stays open and accumulates, then the answer streams in
          under it. Folding the log away hid the only account of what the turn
          did — and because `finalMessageIndex` moves prose between "final"
          and "progress" mid-stream, a paragraph already on screen would drop
          into the folded row the moment a tool part arrived. */}
      <div className="text-ink w-full max-w-none min-w-0 overflow-hidden text-lg leading-8 [overflow-wrap:anywhere]">
        <WorkLogTrace events={content.workEvents} streaming={preAnswer} />
        {streaming && !hasActivity ? (
          <ThinkingRow attempt={retry?.attempt} maxAttempts={retry?.maxAttempts} />
        ) : content.hasFinal ? (
          <section aria-label={t("final.title")}>
            {showFinalLabel ? (
              <div className="text-n600 mb-1 text-xs font-medium">{t("final.title")}</div>
            ) : null}
            <Suspense fallback={<p className="whitespace-pre-wrap">{content.finalText}</p>}>
              <Markdown text={content.finalText} streaming={streaming} />
            </Suspense>
          </section>
        ) : null}
      </div>

      {content.incomplete && !meta.error ? (
        <div className="border-hair bg-n100/50 mt-1 rounded-lg border px-3 py-2">
          <p className="text-n700 text-sm font-medium">{t("final.missingTitle")}</p>
          <p className="text-n600 mt-0.5 text-xs leading-5">{t("final.missingBody")}</p>
        </div>
      ) : null}

      {meta.error ? (
        <InlineErrorCard
          error={meta.error}
          sessionId={sessionId}
          messageId={meta.messageId}
          streaming={streaming}
        />
      ) : null}

      <ResultArtifacts groups={content.resultGroups} verification={content.verification} />

      {(view.patches.length > 0 || view.plans.length > 0) && (
        <div className="mt-2 flex flex-col gap-2">
          {view.patches.map((p) => (
            <PatchChip key={p.id} part={p} sessionId={sessionId} />
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
        messageId={content.finalMessageId ?? meta.messageId}
        content={content.finalText}
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
export function TypingRow({ retry }: { retry?: { attempt: number; maxAttempts: number } }) {
  return (
    <div className="flex w-full min-w-0 flex-col">
      <ThinkingRow attempt={retry?.attempt} maxAttempts={retry?.maxAttempts} />
    </div>
  )
}

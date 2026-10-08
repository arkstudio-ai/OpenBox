// One assistant response, laid out exactly like DEEIX-Chat's message-bot:
// no avatar and no bubble — the turn owns the full column width, with the
// collapsed process / thinking / tool-chain rows stacked on top, then one
// reading column holding the work log and the final answer in order, then the
// semantically grouped artifacts. Tool-step prose stays in the work log rather
// than being concatenated into the answer, but the log is open rather than
// folded: it is the turn's only account of itself.
import { lazy, Suspense, useContext, useMemo } from "react"
import { useTranslation } from "react-i18next"
import type { MessageWithParts } from "@/shared/types/api"
import { buildAssistantContentView } from "../lib/content-view"
import { buildCompactionViews, isCompactionMessage } from "../lib/compaction-view"
import { assistantMessageMeta, buildTurnView, type AssistantTurnMeta, type AssistantTurnOrigin } from "../lib/turn-view"
import { assistantActivity } from "../lib/assistant-activity"
import { hideInternalIds } from "../lib/assistant-text"
import { cn } from "@/shared/lib/cn"
import { useAssistantNames } from "@/shared/appearance/useAssistantNames"
import { AssistantAvatar } from "./AssistantAvatar"
import { AssistantMeta } from "./meta/AssistantMeta"
import { InlineErrorCard } from "./meta/InlineErrorCard"
import { PatchChip } from "./PatchChip"
import { PlanPartCard } from "./PlanPartCard"
import { ProcessTrace } from "./ProcessTrace"
import { CompactionTrace } from "./CompactionTrace"
import { ResultArtifacts } from "./ResultArtifacts"
import { SkillJobReceipts } from "./SkillJobReceipts"
import { StepDivider } from "./StepDivider"
import { ThinkingRow } from "./ThinkingRow"
import { ThinkingTrace } from "./ThinkingTrace"
import { TodoCard } from "./TodoCard"
import { ToolChainTrace } from "./ToolChainTrace"
import { WorkLogTrace } from "./WorkLogTrace"
import { VisibleAssistantAnswer } from "./AssistantReadBoundary"
import { AssistantTaskReceipts } from "./AssistantTaskCard"
import { AssistantMemoryReceipts } from "./AssistantMemoryReceipts"
import { RecalledMemories } from "./RecalledMemories"
import { AssistantReadContext } from "../hooks/assistant-read-context"
import { assistantReplyDuration } from "../lib/assistant-reply-duration"
import { AiGeneratedLabel } from "./AiDisclosure"

const Markdown = lazy(() => import("./Markdown"))

interface Props {
  messages: MessageWithParts[]
  sessionId: string
  meta: AssistantTurnMeta
  /** This is the live turn. */
  streaming: boolean
  /** The current turn is paused for input or queued after an answer. */
  awaitingInput?: boolean
  /** Set while a stalled run is retrying, so the wait can say which try. */
  retry?: { attempt: number; maxAttempts: number }
  /** Abort the run — offered by the task card while one is in flight. */
  onStop?: () => void
  /** This turn holds the conversation's newest task card, so its card is the
   *  one that may be edited. */
  todoEditable?: boolean
  /** A task's result or the daily briefing started this answer (personal assistant). */
  origin?: AssistantTurnOrigin
}

type ContentView = ReturnType<typeof buildAssistantContentView>
type TurnView = ReturnType<typeof buildTurnView>

function showAiLabel(content: ContentView, streaming: boolean): boolean {
  return streaming || content.hasFinal || content.workEvents.length > 0 || content.resultGroups.length > 0
}

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

function answerPresentation(messages: MessageWithParts[], finalMessageId: string | null,
  meta: AssistantTurnMeta, { streaming, mainAssistant }: { streaming: boolean; mainAssistant: boolean }) {
  // On the main assistant page a report and later coordination can share a
  // visual turn: the inputs between them are not user bubbles. The answer's
  // actions and badges still belong to that report, not a later tool step.
  const answer = mainAssistant ? messages.find((message) => message.id === finalMessageId) : undefined
  return answer ? { meta: assistantMessageMeta(answer), streaming: streaming && answer.id === meta.messageId && !answer.finish }
    : { meta, streaming }
}

/** Only the main assistant page provides this context, for its own session. */
function useMainAssistant(sessionId: string): boolean {
  return useContext(AssistantReadContext)?.snapshot.session?.id === sessionId
}

function useReplyTiming(mainAssistant: boolean, answer: MessageWithParts | undefined, stepDuration: number) {
  const { t } = useTranslation("chat")
  if (!mainAssistant) return { process: stepDuration, reply: stepDuration, label: undefined }
  // Several report/coordination runs can share this visual turn. Only the
  // displayed answer owns its Inbox timing; missing boundaries stay unknown.
  return { process: 0, reply: assistantReplyDuration(answer), label: t("assistant.replyDuration") }
}

export function AssistantTurn(props: Props) {
  const mainAssistant = useMainAssistant(props.sessionId)
  return mainAssistant ? <PersonaTurn {...props} /> : <WorkTurn {...props} />
}

/** What the personal assistant is doing while its words are not here yet. */
function PersonaActivity({ label, retry }: { label: string; retry?: { attempt: number; maxAttempts: number } }) {
  const { t } = useTranslation("chat")
  const retrying = Boolean(retry?.attempt && retry.attempt > 0)
  return (
    <div className="flex items-center gap-2 py-1" role="status" aria-live="polite">
      <span className="flex flex-none items-center gap-1" aria-hidden>
        {[0, 1, 2].map((i) => (
          <span key={i} className={cn("animate-pulse-dot size-1.5 rounded-full", retrying ? "bg-sage" : "bg-n600")}
            style={{ animationDelay: `${i * 0.16}s` }} />
        ))}
      </span>
      <span className={cn("text-md", retrying ? "text-sage" : "text-n600")}>
        {retrying ? t("status.retrying", { attempt: retry?.attempt, total: retry?.maxAttempts ?? retry?.attempt }) : label}
      </span>
    </div>
  )
}

/** The personal assistant speaks like a person: its name and face, its words,
 *  and cards for what it set in motion. How it got there (tool calls, context
 *  size, model, tokens) stays off the page. */
function PersonaTurn({ messages, sessionId, meta, streaming, awaitingInput = false, retry, origin }: Props) {
  const { t } = useTranslation("chat")
  const assistantName = useAssistantNames().title
  const replyMessages = useMemo(() => messages.filter((message) => !isCompactionMessage(message)), [messages])
  const parts = useMemo(() => replyMessages.flatMap((message) => message.parts), [replyMessages])
  const view = useMemo(() => buildTurnView(parts), [parts])
  const content = useMemo(() => buildAssistantContentView(messages, streaming, awaitingInput), [messages, streaming, awaitingInput])
  const answer = answerPresentation(replyMessages, content.finalMessageId, meta, { streaming, mainAssistant: true })
  const timing = useReplyTiming(true, replyMessages.find((message) => message.id === answer.meta.messageId), view.durationSec)
  // Older answers may still name internal ids; the page never shows them.
  const finalText = useMemo(() => hideInternalIds(content.finalText), [content.finalText])
  // A turn that is only the conversation tidying its own history has nothing to say.
  if (replyMessages.length === 0) return null
  const preAnswer = streaming && !content.hasFinal
  return (
    <div className="group/msg flex w-full min-w-0 flex-col" data-testid="assistant-persona-turn">
      <div className="mb-1.5 flex items-center gap-2">
        <AssistantAvatar />
        <span className="text-ink text-sm font-medium">{assistantName}</span>
        {origin && <span className="bg-hairsoft text-n700 rounded-full px-2 py-0.5 text-xs">{t(`assistant.origin.${origin}`)}</span>}
        <AiGeneratedLabel visible={showAiLabel(content, streaming)} className="text-xs" />
      </div>
      <div className="min-w-0 ps-8">
        <div className="text-ink w-full max-w-none min-w-0 overflow-hidden text-lg leading-8 [overflow-wrap:anywhere]">
          <WorkLogTrace events={content.workEvents} streaming={preAnswer} />
          {preAnswer ? (
            <PersonaActivity label={t(`assistant.activity.${assistantActivity(view.tools)}`)} retry={retry} />
          ) : content.hasFinal ? (
            <section aria-label={t("final.title")}>
              <VisibleAssistantAnswer messageId={content.finalMessageId}>
                <Suspense fallback={<p className="whitespace-pre-wrap">{finalText}</p>}>
                  <Markdown key={content.finalMessageId} text={finalText} streaming={answer.streaming} />
                </Suspense>
              </VisibleAssistantAnswer>
            </section>
          ) : null}
        </div>
        <AssistantTaskReceipts parts={parts} />
        <AssistantMemoryReceipts parts={parts} />
        <RecalledMemories sessionId={sessionId} messageId={replyMessages[0]?.parent_id} streaming={answer.streaming} />
        {content.incomplete && !meta.error ? (
          <div className="border-hair bg-n100/50 mt-1 rounded-lg border px-3 py-2">
            <p className="text-n700 text-sm font-medium">{t("final.missingTitle")}</p>
            <p className="text-n600 mt-0.5 text-xs leading-5">{t("final.missingBody")}</p>
          </div>
        ) : null}
        {meta.error ? (
          <InlineErrorCard error={meta.error} sessionId={sessionId} messageId={meta.messageId} streaming={streaming} />
        ) : null}
        <ResultArtifacts groups={content.resultGroups} verification={content.verification} />
        <AssistantMeta
          sessionId={sessionId}
          messageId={content.finalMessageId ?? meta.messageId}
          content={finalText}
          tokens={answer.meta.tokens}
          reaction={answer.meta.reaction}
          createdAt={answer.meta.createdAt}
          streaming={answer.streaming}
          durationSec={timing.reply}
          completedDurationLabel={timing.label}
          minimal
        />
      </div>
    </div>
  )
}

function WorkTurn({ messages, sessionId, meta, streaming, awaitingInput = false, retry, onStop, todoEditable }: Props) {
  const { t } = useTranslation("chat")
  const mainAssistant = useMainAssistant(sessionId)
  const replyMessages = useMemo(() => messages.filter((message) => !isCompactionMessage(message)), [messages])
  const compactions = useMemo(() => buildCompactionViews(messages, streaming), [messages, streaming])
  const parts = useMemo(() => replyMessages.flatMap((message) => message.parts), [replyMessages])
  const view = useMemo(() => buildTurnView(parts), [parts])
  const content = useMemo(
    () => buildAssistantContentView(messages, streaming, awaitingInput),
    [messages, streaming, awaitingInput],
  )
  const answer = answerPresentation(replyMessages, content.finalMessageId, meta, { streaming, mainAssistant })
  const timing = useReplyTiming(mainAssistant, replyMessages.find((message) => message.id === answer.meta.messageId), view.durationSec)
  // "Thinking" is the state of having nothing yet — not of having no prose
  // yet. Once reasoning or a tool call has arrived the turn is visibly
  // working, and each of those blocks carries its own live heading, so a
  // second "正在思考中" underneath is both redundant and wrong: it claims the
  // model has not responded when it plainly has.
  const hasActivity = hasTurnActivity(content, view) || compactions.length > 0
  // Per-part activity flags (`thinkingStreaming` flips every time a tool part
  // lands after reasoning; `toolsStreaming` drops in the gap between two
  // calls). Feeding those raw into the trace rows made titles flicker between
  // "正在思考" and "思考完成". Hold each trace live for its whole phase —
  // until the turn starts answering — instead.
  const preAnswer = streaming && !content.hasFinal
  const optimizing = compactions.some((item) => item.status === "running")
  const thinkingLive = preAnswer && !optimizing && (view.thinkingStreaming || view.thinking.length > 0)
  const toolsLive = streaming && !optimizing && (view.toolsStreaming || !content.hasFinal)
  const showFinalLabel = needsFinalLabel(content, view)

  // A compaction arriving before the first reply is already a process, even
  // while the rest of the turn has not arrived (or is outside this page).
  if (replyMessages.length === 0 && compactions.length > 0) {
    return (
      <section aria-label={t("trace.groupTitle")} className="w-full min-w-0">
        {compactions.map((item) => (
          <CompactionTrace key={item.id} item={item} />
        ))}
      </section>
    )
  }

  return (
    <div className="group/msg flex w-full min-w-0 flex-col">
      <section aria-label={t("trace.groupTitle")} className="w-full min-w-0">
        <ProcessTrace
          contextTokens={view.contextTokens}
          durationSec={timing.process}
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
        {/* The task card owns its calls; this row contains the remaining calls. */}
        <ToolChainTrace tools={view.tools} streaming={toolsLive} />
        {compactions.map((item) => (
          <CompactionTrace key={item.id} item={item} />
        ))}
      </section>
      <SkillJobReceipts parts={parts} />
      <AssistantTaskReceipts parts={parts} />

      <AiGeneratedLabel visible={showAiLabel(content, streaming)} className="mt-2 mb-1.5" />

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
            <VisibleAssistantAnswer messageId={content.finalMessageId}>
              <Suspense fallback={<p className="whitespace-pre-wrap">{content.finalText}</p>}>
                <Markdown key={content.finalMessageId} text={content.finalText} streaming={answer.streaming} />
              </Suspense>
            </VisibleAssistantAnswer>
          </section>
        ) : null}
      </div>
      {/* What the assistant remembered or forgot, under the answer it gave. */}
      <AssistantMemoryReceipts parts={parts} />

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
        tokens={answer.meta.tokens}
        reaction={answer.meta.reaction}
        createdAt={answer.meta.createdAt}
        streaming={answer.streaming}
        durationSec={timing.reply}
        completedDurationLabel={timing.label}
      />
    </div>
  )
}

/** Placeholder before the assistant's first part arrives. */
export function TypingRow({ retry, sessionId }: { retry?: { attempt: number; maxAttempts: number }; sessionId?: string }) {
  const { t } = useTranslation("chat")
  const assistantName = useAssistantNames().title
  const persona = useMainAssistant(sessionId ?? "")
  if (persona) {
    return (
      <div className="flex w-full min-w-0 flex-col">
        <div className="mb-1.5 flex items-center gap-2">
          <AssistantAvatar />
          <span className="text-ink text-sm font-medium">{assistantName}</span>
        </div>
        <div className="ps-8"><PersonaActivity label={t("assistant.activity.thinking")} retry={retry} /></div>
      </div>
    )
  }
  return (
    <div className="flex w-full min-w-0 flex-col">
      <ThinkingRow attempt={retry?.attempt} maxAttempts={retry?.maxAttempts} />
    </div>
  )
}

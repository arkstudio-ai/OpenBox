// Cards of the step flow (lite plan §4.3). A card shows the summary the list
// already has; its full content (the user's text, the AI's reply, a tool's
// arguments and output) is read from the record detail only when opened.
import { lazy, Suspense, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { ChevronRight, ExternalLink, Paperclip } from "lucide-react"
import { Link } from "react-router"
import { cn } from "@/shared/lib/cn"
import { formatTokens } from "@/shared/lib/format"
import { Spinner } from "@/shared/ui/Spinner"
import { StatusPill } from "@/shared/ui/StatusPill"
import { useRecordDetail } from "../../api/queries"
import { JsonTree } from "../../components/inspector/JsonTree"
import { TextBlock } from "../../components/inspector/TextBlock"
import { labelKey, RECORD_STATUS_LABELS, RECORD_STATUS_TONES } from "../../constants/labels"
import type { RecordBlock, Seq, TrajectoryRecord } from "../../types/protocol"
import { formatDuration, formatInstant } from "../../utils/time"
import { groupLabel, resolveTool } from "../labels"
import { isFailed, isOpen, type FlowItem } from "../steps"
import type { LiteLabels, LiteStep } from "../types"

const MarkdownRenderer = lazy(() => import("../../components/inspector/MarkdownRenderer"))

export interface StepEnv {
  sessionId: string
  throughSeq: Seq
  labels: LiteLabels
  /** Link to the professional view selecting one record. */
  rawHref: (recordId: string) => string
}

const REASONING = new Set(["reasoning", "reasoning_text"])
const TEXT = new Set(["text", "output_text"])

function hasAttachment(value: unknown, depth = 0): boolean {
  if (depth > 6 || value === null || typeof value !== "object") return false
  if (Array.isArray(value)) return value.some((child) => hasAttachment(child, depth + 1))
  const record = value as Record<string, unknown>
  if ("$media" in record || "$payload" in record) return true
  return Object.values(record).some((child) => hasAttachment(child, depth + 1))
}

function RecordStatus({ step }: { step: LiteStep }) {
  const { t } = useTranslation("admin-trajectories")
  if (!step.status) return null
  return (
    <StatusPill tone={RECORD_STATUS_TONES[step.status] ?? "muted"} title={step.status_reason ?? undefined}>
      {t(labelKey(RECORD_STATUS_LABELS, step.status, "status.other"), { value: step.status })}
    </StatusPill>
  )
}

function Meta({ step }: { step: LiteStep }) {
  const { i18n } = useTranslation("admin-trajectories")
  const duration = formatDuration(step.duration_ms, i18n.language)
  const at = formatInstant(step.started_at ?? step.finished_at, "local", i18n.language)
  return (
    <span className="text-n500 text-2xs" title={at ?? undefined}>
      {[at?.slice(11, 19), duration].filter(Boolean).join(" · ")}
    </span>
  )
}

function RawLink({ recordId, env }: { recordId: string; env: StepEnv }) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <Link
      to={env.rawHref(recordId)}
      className="text-n500 hover:text-a700 text-2xs inline-flex items-center gap-1"
      onClick={(event) => event.stopPropagation()}
    >
      {t("lite.turn.rawRecord")}
      <ExternalLink size={10} aria-hidden />
    </Link>
  )
}

function Value({ value, mono = true }: { value: unknown; mono?: boolean }) {
  if (value === null || value === undefined) return null
  if (typeof value === "string") return <TextBlock text={value} mono={mono} initialChars={4000} />
  return <JsonTree value={value} openDepth={1} />
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-n500 text-2xs">{label}</span>
      <div className="min-w-0 text-xs">{children}</div>
    </div>
  )
}

function Markdown({ text }: { text: string }) {
  return (
    <Suspense fallback={<Spinner className="size-4" />}>
      <MarkdownRenderer text={text} />
    </Suspense>
  )
}

function UserInputBody({ step, data }: { step: LiteStep; data: Record<string, unknown> }) {
  const { t } = useTranslation("admin-trajectories")
  const text = typeof data.text === "string" ? data.text : (step.preview ?? "")
  return (
    <div className="flex flex-col gap-2">
      <p className="text-ink text-sm leading-6 break-words whitespace-pre-wrap">{text}</p>
      {hasAttachment(data) && (
        <span className="text-n600 text-2xs inline-flex items-center gap-1">
          <Paperclip size={11} aria-hidden />
          {t("lite.turn.attachmentsInPro")}
        </span>
      )}
    </div>
  )
}

function AssistantBody({ blocks }: { blocks: RecordBlock[] }) {
  const { t } = useTranslation("admin-trajectories")
  const text = blocks
    .filter((block) => TEXT.has(String(block.type)))
    .map((block) => block.text)
    .join("")
  const reasoning = blocks
    .filter((block) => REASONING.has(String(block.type)))
    .map((block) => block.text)
    .join("\n")
  return (
    <div className="flex flex-col gap-2">
      {reasoning && (
        <details className="text-n600 text-xs">
          <summary className="cursor-pointer">{t("lite.turn.reasoning")}</summary>
          <TextBlock text={reasoning} mono={false} initialChars={4000} />
        </details>
      )}
      {text ? <Markdown text={text} /> : <p className="text-n500 text-xs">{t("lite.turn.noReplyText")}</p>}
    </div>
  )
}

/** Two labelled values side by side; a missing one reads as a dash or the given placeholder. */
function Pair({
  left,
  right,
  leftValue,
  rightValue,
  placeholder,
  mono = true,
}: {
  left: string
  right: string
  leftValue: unknown
  rightValue: unknown
  placeholder?: string
  mono?: boolean
}) {
  const { t } = useTranslation("admin-trajectories")
  const missing = <span className="text-n500">{placeholder ?? t("common.dash")}</span>
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Field label={left}>
        {leftValue === undefined ? missing : <Value value={leftValue} mono={mono} />}
      </Field>
      <Field label={right}>
        {rightValue === undefined ? missing : <Value value={rightValue} mono={mono} />}
      </Field>
    </div>
  )
}

/** The content of one record once its detail is loaded, by kind. */
function Body({ step, record }: { step: LiteStep; record: TrajectoryRecord }) {
  const { t } = useTranslation("admin-trajectories")
  const data = record.data ?? {}
  switch (step.step_type) {
    case "user_input":
      return <UserInputBody step={step} data={data} />
    case "assistant":
      return <AssistantBody blocks={record.blocks ?? []} />
    case "tool":
      return (
        <Pair
          left={t("lite.turn.didWhat")}
          right={t("lite.turn.result")}
          leftValue={data.requested_arguments ?? data.arguments ?? data.effective_arguments}
          rightValue={data.model_output ?? data.output ?? data.result}
        />
      )
    case "question":
      return (
        <Pair
          left={t("lite.turn.asked")}
          right={t("lite.turn.answered")}
          leftValue={data.questions ?? step.preview}
          rightValue={data.answers}
          placeholder={t("lite.turn.noAnswerYet")}
          mono={false}
        />
      )
    case "permission":
      return (
        <Pair
          left={t("lite.turn.permissionAsked")}
          right={t("lite.turn.decision")}
          leftValue={data.reason ?? data.title ?? step.preview}
          rightValue={data.decision ?? data.status}
          placeholder={t("lite.turn.noAnswerYet")}
          mono={false}
        />
      )
    default:
      return <JsonTree value={data} openDepth={1} />
  }
}

function Detail({ step, env }: { step: LiteStep; env: StepEnv }) {
  const { t } = useTranslation("admin-trajectories")
  const detail = useRecordDetail(env.sessionId, env.throughSeq, step.record_id, {
    expand: "full",
    enabled: true,
  })
  if (detail.isLoading) {
    return (
      <div className="flex items-center gap-2 py-2">
        <Spinner className="size-4" />
        <span className="sr-only">{t("lite.turn.loadingStep")}</span>
      </div>
    )
  }
  if (detail.error || !detail.data)
    return <p className="text-danger text-xs">{t("lite.turn.stepUnavailable")}</p>
  return <Body step={step} record={detail.data.record} />
}

/** One step: header with the action, status and timing; its content below, loaded when open. */
export function StepCard({
  step,
  env,
  defaultOpen = false,
  number,
}: {
  step: LiteStep
  env: StepEnv
  defaultOpen?: boolean
  number?: number
}) {
  const { t } = useTranslation("admin-trajectories")
  const [open, setOpen] = useState(defaultOpen)
  const failed = isFailed(step)
  const live = isOpen(step)
  const title = (() => {
    switch (step.step_type) {
      case "user_input":
        return t("lite.turn.step.userInput")
      case "assistant":
        return t("lite.turn.step.assistant")
      case "tool":
        return step.label || resolveTool(step.title, env.labels).label
      case "question":
        return t("lite.turn.step.question")
      case "permission":
        return t("lite.turn.step.permission")
      case "takeover":
        return t("lite.turn.step.takeover")
      case "job":
        return t("lite.turn.step.job", { name: step.title })
      case "artifact":
        return t("lite.turn.step.artifact", { name: step.title })
      default:
        return step.title
    }
  })()
  const subtitle =
    step.step_type === "tool" && step.preview
      ? step.preview
      : step.step_type === "assistant"
        ? null
        : step.preview
  return (
    <article
      data-testid="lite-step"
      data-step-type={step.step_type}
      data-status={step.status ?? ""}
      className={cn(
        "border-hair bg-card rounded-xl border",
        failed && "border-dangerink/40 ring-dangersoft ring-3",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 px-4 py-3 text-start"
      >
        <span
          aria-hidden
          className={cn(
            "text-2xs mt-0.5 flex size-5 flex-none items-center justify-center rounded-full font-semibold",
            failed ? "bg-dangerink text-bg" : live ? "bg-s100 text-s800" : "bg-ink text-bg",
          )}
        >
          {number ?? "·"}
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-ink text-sm font-semibold">{title}</span>
            {step.step_type !== "user_input" && <RecordStatus step={step} />}
            <Meta step={step} />
          </span>
          {!open && subtitle && (
            <span className="text-n600 line-clamp-2 text-xs break-words">{subtitle}</span>
          )}
          {failed && step.status_reason && (
            <span className="bg-dangersoft text-dangerink text-2xs rounded-md px-2 py-1 font-mono break-words whitespace-pre-wrap">
              {step.status_reason}
            </span>
          )}
        </span>
        <ChevronRight
          size={14}
          aria-hidden
          className={cn("text-n500 mt-1 flex-none transition-transform", open && "rotate-90")}
        />
      </button>
      {open && (
        <div className="border-hair flex flex-col gap-3 border-t px-4 py-3">
          <Detail step={step} env={env} />
          <div className="flex justify-end">
            <RawLink recordId={step.record_id} env={env} />
          </div>
        </div>
      )}
    </article>
  )
}

/** Consecutive low-value tools of one group: one line, the steps on request. */
export function GroupCard({ item, env }: { item: Extract<FlowItem, { kind: "group" }>; env: StepEnv }) {
  const { t } = useTranslation("admin-trajectories")
  const [open, setOpen] = useState(false)
  const label = groupLabel(item.group, env.labels) || item.label
  return (
    <div data-testid="lite-step-group" className="border-hair rounded-xl border border-dashed">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="text-n600 flex w-full items-center gap-2 px-4 py-2 text-start text-xs"
      >
        <ChevronRight
          size={12}
          aria-hidden
          className={cn("flex-none transition-transform", open && "rotate-90")}
        />
        <span className="text-n800 font-medium">
          {t("lite.turn.groupTitle", { label, count: item.steps.length })}
        </span>
        <span>
          {item.failed ? t("lite.turn.groupFailed", { count: item.failed }) : t("lite.turn.groupAllOk")}
        </span>
      </button>
      {open && (
        <div className="flex flex-col gap-2 px-3 pb-3">
          {item.steps.map((step) => (
            <StepCard key={step.record_id} step={step} env={env} />
          ))}
        </div>
      )}
    </div>
  )
}

/** A sub-agent: its name and verdict; the steps it ran fold inside. */
export function AgentCard({
  item,
  env,
  number,
}: {
  item: Extract<FlowItem, { kind: "agent" }>
  env: StepEnv
  number?: number
}) {
  const { t } = useTranslation("admin-trajectories")
  const [open, setOpen] = useState(false)
  const step = item.step
  return (
    <article data-testid="lite-step" data-step-type="agent" className="border-hair bg-card rounded-xl border">
      <div className="flex items-start gap-3 px-4 py-3">
        <span
          aria-hidden
          className="bg-ink text-bg text-2xs mt-0.5 flex size-5 flex-none items-center justify-center rounded-full font-semibold"
        >
          {number ?? "·"}
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-ink text-sm font-semibold">
              {t("lite.turn.step.agent", { name: step.title })}
            </span>
            <RecordStatus step={step} />
            <Meta step={step} />
            <span className="text-n500 text-2xs">
              {t("lite.turn.agentSteps", { count: item.children.length })}
            </span>
          </span>
          {step.result_preview && (
            <span className="text-n700 line-clamp-2 text-xs">{step.result_preview}</span>
          )}
          <span className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen((value) => !value)}
              className="text-a700 inline-flex items-center gap-1 text-xs"
            >
              <ChevronRight
                size={12}
                aria-hidden
                className={cn("transition-transform", open && "rotate-90")}
              />
              {open
                ? t("lite.turn.hideAgentSteps")
                : t("lite.turn.showAgentSteps", { count: item.children.length })}
            </button>
            <RawLink recordId={step.record_id} env={env} />
          </span>
        </div>
      </div>
      {open && (
        <div className="border-hair flex flex-col gap-2 border-t px-3 py-3">
          <Flow items={item.children} env={env} />
        </div>
      )}
    </article>
  )
}

export function GapNotice() {
  const { t } = useTranslation("admin-trajectories")
  return (
    <p role="note" className="bg-a200 text-n800 rounded-lg px-3 py-2 text-xs">
      {t("lite.turn.gap")}
    </p>
  )
}

/** The ordered cards of a flow; user input, AI replies and failures start open, the rest closed. */
export function Flow({ items, env }: { items: FlowItem[]; env: StepEnv }) {
  // Cards are numbered; folded groups and gap notices are not.
  const numbered = new Map<FlowItem, number>()
  for (const item of items)
    if (item.kind === "step" || item.kind === "agent") numbered.set(item, numbered.size + 1)
  return (
    <>
      {items.map((item) => {
        switch (item.kind) {
          case "gap":
            return <GapNotice key={item.step.record_id} />
          case "group":
            return <GroupCard key={item.steps[0]!.record_id} item={item} env={env} />
          case "agent":
            return <AgentCard key={item.step.record_id} item={item} env={env} number={numbered.get(item)} />
          default:
            return (
              <StepCard
                key={item.step.record_id}
                step={item.step}
                env={env}
                number={numbered.get(item)}
                defaultOpen={
                  item.step.step_type === "user_input" ||
                  item.step.step_type === "assistant" ||
                  isFailed(item.step)
                }
              />
            )
        }
      })}
    </>
  )
}

export function UsageLine({ input, output }: { input: number | null; output: number | null }) {
  const { t } = useTranslation("admin-trajectories")
  if (input === null || output === null) return <>{t("common.notRecorded")}</>
  return <>{t("list.tokens", { input: formatTokens(input), output: formatTokens(output) })}</>
}

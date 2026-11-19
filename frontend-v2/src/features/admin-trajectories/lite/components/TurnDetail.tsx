// 这一轮做了什么: the detail a turn of the conversation view unfolds in place —
// its steps as one line each, what its credits were charged for, and every
// generation prompt. Read only once the turn is opened; a step's full content
// stays on the turn page (lite plan §4.3), one link away.
import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { ExternalLink } from "lucide-react"
import { Link } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { formatCredits } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { EMPTY_DETAIL_PARAMS, serializeDetailParams } from "../../utils/params"
import { formatDuration } from "../../utils/time"
import { useTurnDetail } from "../api"
import { groupLabel, resolveTool } from "../labels"
import { buildFlow, isFailed, stepSubtitle, stepTitle, type FlowItem } from "../steps"
import type { CreditPart, LiteLabels, LiteStep } from "../types"
import { MediaPromptsSection } from "./MediaPrompts"
import { GapNotice, RecordStatus } from "./StepCards"

const ROW = "grid grid-cols-[4.5rem_minmax(0,1fr)_auto_4.5rem] items-baseline gap-3 px-4 py-2.5"

function Row({
  iso,
  title,
  subtitle,
  status,
  durationMs,
  failed = false,
  reason,
}: {
  iso: string | null
  title: string
  subtitle?: string | null
  status?: React.ReactNode
  durationMs: number | null
  failed?: boolean
  reason?: string | null
}) {
  const { i18n } = useTranslation("admin-trajectories")
  const clock = iso
    ? new Intl.DateTimeFormat(i18n.language, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      }).format(new Date(iso))
    : ""
  return (
    <li
      className={cn(ROW, "border-hair border-t text-xs first:border-t-0", failed && "bg-dangersoft/40")}
      data-testid="replay-step"
    >
      <span className="text-n500 tabular-nums">{clock}</span>
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-ink text-sm font-medium break-words">{title}</span>
        {subtitle && <span className="text-n600 line-clamp-2 break-words">{subtitle}</span>}
        {failed && reason && <span className="text-dangerink break-words whitespace-pre-wrap">{reason}</span>}
      </span>
      <span>{status}</span>
      <span className="text-n500 text-end tabular-nums">
        {formatDuration(durationMs, i18n.language) ?? ""}
      </span>
    </li>
  )
}

function total(steps: LiteStep[]): number | null {
  const known = steps.map((step) => step.duration_ms).filter((ms): ms is number => ms != null)
  return known.length ? known.reduce((sum, ms) => sum + ms, 0) : null
}

function Rows({ items, labels }: { items: FlowItem[]; labels: LiteLabels }) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <>
      {items.map((item) => {
        switch (item.kind) {
          case "gap":
            return (
              <li key={item.step.record_id} className="border-hair border-t px-4 py-2 first:border-t-0">
                <GapNotice />
              </li>
            )
          case "group":
            return (
              <Row
                key={item.steps[0]!.record_id}
                iso={item.steps[0]!.started_at}
                title={t("lite.turn.groupTitle", {
                  label: groupLabel(item.group, labels) || item.label,
                  count: item.steps.length,
                })}
                subtitle={
                  item.failed ? t("lite.turn.groupFailed", { count: item.failed }) : t("lite.turn.groupAllOk")
                }
                durationMs={total(item.steps)}
                failed={item.failed > 0}
              />
            )
          case "agent":
            return (
              <Row
                key={item.step.record_id}
                iso={item.step.started_at}
                title={stepTitle(item.step, labels, t)}
                subtitle={[
                  t("lite.turn.agentSteps", { count: item.children.length }),
                  item.step.result_preview,
                ]
                  .filter(Boolean)
                  .join(" · ")}
                status={<RecordStatus step={item.step} />}
                durationMs={item.step.duration_ms}
                failed={isFailed(item.step)}
                reason={item.step.status_reason}
              />
            )
          default:
            // The person's message is the bubble right above; no need to repeat it as a step.
            if (item.step.step_type === "user_input") return null
            return (
              <Row
                key={item.step.record_id}
                iso={item.step.started_at ?? item.step.finished_at}
                title={stepTitle(item.step, labels, t)}
                // A question's wording is already in the conversation above; its preview is raw JSON.
                subtitle={item.step.step_type === "question" ? null : stepSubtitle(item.step)}
                status={<RecordStatus step={item.step} />}
                durationMs={item.step.duration_ms}
                failed={isFailed(item.step)}
                reason={item.step.status_reason}
              />
            )
        }
      })}
    </>
  )
}

/** "生成视频 ×3 162 · 模型对话 12": a usage kind is named by the tool dictionary when it is a tool. */
function Breakdown({ parts, labels }: { parts: CreditPart[]; labels: LiteLabels }) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <p className="text-n600 text-xs" data-testid="replay-credit-breakdown">
      <span className="text-n500">{t("lite.replay.breakdown")}：</span>
      {parts
        .map((part) => {
          const tool = resolveTool(part.kind, labels)
          const label = tool.known
            ? tool.label
            : t(`lite.usageKind.${part.kind}`, { defaultValue: part.kind })
          const credits = formatCredits(part.credits)
          return part.count > 1
            ? t("lite.replay.breakdownItemTimes", { label, count: part.count, credits })
            : t("lite.replay.breakdownItem", { label, credits })
        })
        .join(" · ")}
    </p>
  )
}

export function TurnDetailPanel({
  userId,
  sessionId,
  turnId,
  back,
  labels,
}: {
  userId: string
  sessionId: string
  turnId: string
  back: string
  labels: LiteLabels
}) {
  const { t } = useTranslation("admin-trajectories")
  const errorMessage = useApiErrorMessage()
  const detail = useTurnDetail(sessionId, turnId)
  const flow = useMemo(
    () => (detail.data ? buildFlow(detail.data.steps, detail.data.summary.main_agent_id, labels) : null),
    [detail.data, labels],
  )
  if (detail.isLoading) {
    return (
      <div className="flex items-center gap-2 px-1 py-3" data-testid="replay-detail-loading">
        <Spinner className="size-4" />
        <span className="sr-only">{t("lite.turn.loading")}</span>
      </div>
    )
  }
  if (detail.error || !detail.data || !flow) {
    return (
      <p role="alert" className="text-danger px-1 py-2 text-xs">
        {detail.error ? errorMessage(detail.error) : t("lite.turn.stepUnavailable")}
      </p>
    )
  }
  const { summary } = detail.data
  const proHref = (record: string | null) =>
    paths.adminTrajectorySession(
      sessionId,
      serializeDetailParams({ ...EMPTY_DETAIL_PARAMS, at: summary.last_seq, record }),
    )
  const turnHref = paths.adminTrajectoryTurn(
    userId,
    sessionId,
    turnId,
    back ? new URLSearchParams({ back }).toString() : undefined,
  )
  const rows = flow.items.filter((item) => item.kind !== "step" || item.step.step_type !== "user_input")
  const parts = summary.credit_breakdown ?? []
  return (
    <div className="flex flex-col gap-3" data-testid="replay-detail">
      <div className="border-hair bg-surface overflow-hidden rounded-xl border">
        <h3 className="border-hair text-ink border-b px-4 py-2.5 text-xs font-semibold">
          {t("lite.replay.detailTitle")}
        </h3>
        {rows.length ? (
          <div className="overflow-x-auto">
            <ul className="min-w-[32rem]">
              <Rows items={rows} labels={labels} />
            </ul>
          </div>
        ) : (
          <p className="text-n500 px-4 py-3 text-xs">{t("lite.turn.noSteps")}</p>
        )}
        <div className="border-hair flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t px-4 py-2.5">
          {parts.length ? <Breakdown parts={parts} labels={labels} /> : <span />}
          <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs font-medium">
            <Link to={turnHref} className="text-a700 hover:underline" data-testid="replay-full-steps">
              {t("lite.replay.fullSteps")}
            </Link>
            <Link to={proHref(null)} className="text-n600 inline-flex items-center gap-1 hover:underline">
              {t("lite.turn.technicalDetails")}
              <ExternalLink size={11} aria-hidden />
            </Link>
          </span>
        </div>
      </div>
      <MediaPromptsSection
        prompts={detail.data.media_prompts ?? []}
        mainAgentId={summary.main_agent_id}
        rawHref={(recordId) => proHref(recordId)}
      />
    </div>
  )
}

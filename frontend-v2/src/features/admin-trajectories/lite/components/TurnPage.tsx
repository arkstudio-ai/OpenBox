import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { ExternalLink } from "lucide-react"
import { Link, useSearchParams } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { AccessNotice } from "../../components/AccessNotice"
import { useTrajectoryAccess } from "../../stores/access"
import { EMPTY_DETAIL_PARAMS, serializeDetailParams } from "../../utils/params"
import { formatDuration, formatInstant } from "../../utils/time"
import { useLiteLabels, useTurnDetail } from "../api"
import { conclusion, turnStatusTone } from "../labels"
import { buildFlow } from "../steps"
import { Duration, TurnStatus } from "./bits"
import { ModeToggle } from "./ModeToggle"
import { Flow, UsageLine, type StepEnv } from "./StepCards"

const EDGE: Record<string, string> = {
  danger: "border-s-dangerink",
  warn: "border-s-a700",
  accent: "border-s-a700",
  ok: "border-s-s800",
  muted: "border-s-hair",
}

function waitedFor(
  summary: { status: string; last_activity_at: string | null },
  locale: string,
): string | null {
  if (summary.status !== "waiting" || !summary.last_activity_at) return null
  return formatDuration(Date.now() - new Date(summary.last_activity_at).getTime(), locale)
}

/** 轮详情: the verdict, the numbers, and what happened as a step flow. */
export function TrajectoryTurnPage({
  userId,
  sessionId,
  turnId,
}: {
  userId: string
  sessionId: string
  turnId: string
}) {
  const { t, i18n } = useTranslation("admin-trajectories")
  const errorMessage = useApiErrorMessage()
  const [search] = useSearchParams()
  const denied = useTrajectoryAccess((s) => s.denied)
  const detail = useTurnDetail(sessionId, turnId)
  const labels = useLiteLabels()
  const back = search.get("back") ?? ""
  const flow = useMemo(
    () => (detail.data ? buildFlow(detail.data.steps, detail.data.summary.main_agent_id, labels) : null),
    [detail.data, labels],
  )

  if (denied) return <AccessNotice denial={denied} />
  if (detail.isLoading) {
    return (
      <div
        className="flex items-center justify-center gap-2 py-16"
        data-testid="trajectory-lite-turn-loading"
      >
        <Spinner className="size-5" />
        <span className="sr-only">{t("lite.turn.loading")}</span>
      </div>
    )
  }
  if (detail.error || !detail.data || !flow) {
    return (
      <p role="alert" className="text-danger py-10 text-center text-sm">
        {detail.error ? errorMessage(detail.error) : t("lite.turn.stepUnavailable")}
      </p>
    )
  }
  const { summary, steps, through_seq } = detail.data
  const proHref = (record: string | null) =>
    paths.adminTrajectorySession(
      sessionId,
      serializeDetailParams({ ...EMPTY_DETAIL_PARAMS, at: summary.last_seq, record }),
    )
  const env: StepEnv = {
    sessionId,
    throughSeq: through_seq,
    labels,
    rawHref: (recordId) => proHref(recordId),
  }
  const tone = turnStatusTone(summary.status)
  const verdict = conclusion(summary, labels, t, waitedFor(summary, i18n.language))
  const started = formatInstant(summary.started_at, "local", i18n.language)
  const finished = formatInstant(summary.finished_at, "local", i18n.language)
  const thinking = steps.filter((step) => step.step_type === "assistant").length
  const userHref = paths.adminTrajectoryUser(userId, back || undefined)

  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-lite-turn">
      <nav aria-label={t("lite.breadcrumb")} className="text-n500 flex flex-wrap items-center gap-2 text-xs">
        <Link to={paths.adminTrajectories()} className="hover:underline">
          {t("lite.users.title")}
        </Link>
        <span aria-hidden>/</span>
        <Link to={userHref} className="hover:underline">
          {t("lite.turn.backToUser")}
        </Link>
        <span aria-hidden>/</span>
        <span className="text-ink">{summary.session_title || t("list.untitled")}</span>
        <span className="flex-1" />
        <ModeToggle mode="lite" />
      </nav>

      <header
        className={cn("border-hair bg-card flex flex-col gap-3 rounded-xl border border-s-4 p-4", EDGE[tone])}
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <TurnStatus status={summary.status} reason={summary.status_reason} />
              <h2 className="text-ink text-base font-semibold break-words" data-testid="lite-verdict">
                {verdict}
              </h2>
            </div>
            <p className="text-n500 text-xs">
              {t("lite.turn.meta", {
                session: summary.session_title || t("list.untitled"),
                started: started ?? t("common.notRecorded"),
                finished: finished ?? t("lite.turn.notFinished"),
              })}
            </p>
          </div>
          <Link
            to={proHref(null)}
            className="bg-ink text-bg inline-flex flex-none items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-medium"
          >
            {t("lite.turn.technicalDetails")}
            <ExternalLink size={12} aria-hidden />
          </Link>
        </div>
        <dl className="border-hair text-n800 flex flex-wrap gap-x-7 gap-y-2 border-t pt-3 text-xs">
          <div className="flex flex-col">
            <dt className="text-n500 text-2xs">{t("lite.turns.column.duration")}</dt>
            <dd className="font-medium">
              <Duration ms={summary.duration_ms} />
            </dd>
          </div>
          <div className="flex flex-col">
            <dt className="text-n500 text-2xs">{t("lite.turn.steps")}</dt>
            <dd className="font-medium">
              {t("lite.turn.stepsCount", { count: flow.items.length })}
              {summary.tool_failures > 0 &&
                ` · ${t("lite.turn.failedSteps", { count: summary.tool_failures })}`}
              {flow.retries > 0 && ` · ${t("lite.turn.retries", { count: flow.retries })}`}
            </dd>
          </div>
          <div className="flex flex-col">
            <dt className="text-n500 text-2xs">{t("lite.turn.replies")}</dt>
            <dd className="font-medium">{t("lite.turn.repliesCount", { count: thinking })}</dd>
          </div>
          <div className="flex flex-col">
            <dt className="text-n500 text-2xs">{t("list.column.usage")}</dt>
            <dd className="font-medium">
              <UsageLine input={summary.input_tokens} output={summary.output_tokens} />
            </dd>
          </div>
          <div className="flex flex-col">
            <dt className="text-n500 text-2xs">{t("lite.turn.credits")}</dt>
            <dd className="text-n500 font-medium">{t("lite.turn.creditsLater")}</dd>
          </div>
        </dl>
      </header>

      <section className="flex flex-col gap-2.5" aria-label={t("lite.turn.flow")}>
        {flow.items.length ? (
          <Flow items={flow.items} env={env} />
        ) : (
          <p className="text-n500 py-6 text-center text-sm">{t("lite.turn.noSteps")}</p>
        )}
      </section>
    </div>
  )
}

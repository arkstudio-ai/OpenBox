// 对话 (lite plan §15): one session read top to bottom like a shared chat — the
// person's messages, the assistant's replies, questions, files sent. Under each
// turn sits what it cost in credits, and the turn's work unfolds in place.
import { lazy, Suspense, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown, Coins, ExternalLink, FileText, Paperclip } from "lucide-react"
import { Link, useSearchParams } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { AccessNotice } from "../../components/AccessNotice"
import { useTrajectoryAccess } from "../../stores/access"
import { EMPTY_DETAIL_PARAMS, serializeDetailParams } from "../../utils/params"
import { useLiteLabels, useSessionReplay } from "../api"
import { formatCredits } from "@/shared/lib/format"
import { formatDuration } from "../../utils/time"
import type { LiteLabels, ReplayEvent, ReplayTurn } from "../types"
import { BUTTON, Clock, TurnStatus } from "./bits"
import { ModeToggle } from "./ModeToggle"
import { TurnDetailPanel } from "./TurnDetail"

const MarkdownRenderer = lazy(() => import("../../components/inspector/MarkdownRenderer"))
/** Pages read ahead at most while looking for the turn named in the URL. */
const SEEK_PAGES = 20

function questionLines(questions: unknown): string[] {
  if (typeof questions === "string") {
    try {
      return questionLines(JSON.parse(questions))
    } catch {
      return [questions]
    }
  }
  if (!Array.isArray(questions)) return []
  return questions
    .map((item) =>
      typeof item === "string"
        ? item
        : item && typeof item === "object" && typeof (item as { question?: unknown }).question === "string"
          ? (item as { question: string }).question
          : "",
    )
    .filter(Boolean)
}

function answerText(answers: unknown): string | null {
  if (answers === null || answers === undefined) return null
  if (typeof answers === "string") return answers
  if (Array.isArray(answers))
    return answers.map((answer) => (typeof answer === "string" ? answer : JSON.stringify(answer))).join("；")
  return JSON.stringify(answers)
}

function Event({ event }: { event: ReplayEvent }) {
  const { t } = useTranslation("admin-trajectories")
  switch (event.type) {
    case "user":
      return (
        <div className="flex justify-end" data-testid="replay-user">
          <div className="bg-hairsoft text-ink flex max-w-[80%] flex-col gap-1 rounded-3xl px-4 py-2.5">
            <p className="text-[15px] leading-7 break-words whitespace-pre-wrap">
              {event.text ?? t("common.dash")}
            </p>
            {event.attachments && (
              <span className="text-n600 text-2xs inline-flex items-center gap-1">
                <Paperclip size={11} aria-hidden />
                {t("lite.turn.attachmentsInPro")}
              </span>
            )}
          </div>
        </div>
      )
    case "reply":
      if (event.status === "failed")
        return (
          <p className="text-dangerink text-xs" data-testid="replay-reply-failed">
            {event.status_reason
              ? t("lite.verdict.failedReply", { reason: event.status_reason })
              : t("lite.verdict.failedReplyNoReason")}
          </p>
        )
      if (!event.text) return null
      return (
        <div className="text-ink min-w-0 text-[15px] leading-7" data-testid="replay-reply">
          <Suspense fallback={<Spinner className="size-4" />}>
            <MarkdownRenderer text={event.text} />
          </Suspense>
        </div>
      )
    case "question": {
      const lines = questionLines(event.questions)
      const answer = answerText(event.answers)
      return (
        <div className="border-a700/40 bg-card flex max-w-[85%] flex-col gap-1.5 rounded-xl border border-dashed px-3.5 py-2.5 text-xs">
          <span className="text-n500 text-2xs">{t("lite.turn.step.question")}</span>
          {lines.map((line, index) => (
            <p key={index} className="text-ink break-words whitespace-pre-wrap">
              {line}
            </p>
          ))}
          <p className="text-n700">
            <span className="text-n500">{t("lite.turn.answered")}：</span>
            {answer ?? t("lite.turn.noAnswerYet")}
          </p>
        </div>
      )
    }
    case "artifact":
      return (
        <div className="text-n700 inline-flex items-center gap-1.5 text-xs">
          <FileText size={12} aria-hidden />
          {t("lite.turn.step.artifact", { name: event.title ?? "" })}
        </div>
      )
    default:
      return null
  }
}

function Turn({
  turn,
  userId,
  back,
  labels,
  highlighted,
  open,
  onToggle,
}: {
  turn: ReplayTurn
  userId: string
  back: string
  labels: LiteLabels
  highlighted: boolean
  open: boolean
  onToggle: () => void
}) {
  const { t, i18n } = useTranslation("admin-trajectories")
  const { summary } = turn
  // Prompts are part of the turn's detail now; the conversation shows what the person saw.
  const events = turn.events.filter((event) => event.type !== "media_prompt")
  const steps = Object.values(summary.tool_counts).reduce((sum, count) => sum + count.total, 0)
  const duration = formatDuration(summary.duration_ms, i18n.language)
  const panel = `turn-detail-${summary.turn_id}`
  return (
    <section
      id={`turn-${summary.turn_id}`}
      className={cn(
        "flex scroll-mt-4 flex-col gap-4 rounded-2xl",
        highlighted && "ring-a700/40 -m-4 p-4 ring-1",
      )}
      data-testid="replay-turn"
      data-status={summary.status}
    >
      {events.map((event) => (
        <Event key={event.record_id} event={event} />
      ))}
      <div
        className="text-n600 flex flex-wrap items-center gap-x-3.5 gap-y-1 text-xs"
        data-testid="replay-turn-line"
      >
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full py-1 ps-2 pe-3 font-semibold",
            summary.credits == null ? "bg-hairsoft text-n600" : "bg-a100 text-ink",
          )}
          data-testid="replay-turn-credits"
        >
          <Coins size={13} aria-hidden />
          {summary.credits == null
            ? t("lite.replay.turnUncharged")
            : t("lite.replay.turnCredits", { credits: formatCredits(summary.credits) })}
        </span>
        {summary.status !== "completed" && (
          <TurnStatus status={summary.status} reason={summary.status_reason} />
        )}
        {duration && <span>{t("lite.replay.took", { duration })}</span>}
        {steps > 0 && <span>{t("lite.turn.stepsCount", { count: steps })}</span>}
        <Clock iso={summary.started_at} />
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={panel}
          className="text-a700 ms-auto inline-flex min-h-9 items-center gap-1 font-semibold hover:underline"
          data-testid="replay-turn-toggle"
        >
          {open ? t("lite.replay.hideDetail") : t("lite.replay.showDetail")}
          <ChevronDown size={14} aria-hidden className={cn("transition-transform", open && "rotate-180")} />
        </button>
      </div>
      {open && (
        <div id={panel}>
          <TurnDetailPanel
            userId={userId}
            sessionId={summary.session_id}
            turnId={summary.turn_id}
            back={back}
            labels={labels}
          />
        </div>
      )}
    </section>
  )
}

function SessionHeader({
  title,
  who,
  total,
  loaded,
  credits,
  failedTurns,
  proHref,
}: {
  title: string | null
  who: string
  total: number
  loaded: number
  credits: string | null | undefined
  failedTurns: number
  proHref: string
}) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <header className="border-hair flex flex-wrap items-start justify-between gap-3 border-b pb-5">
      <div className="flex min-w-0 flex-col gap-2">
        <h2 className="text-ink text-2xl leading-tight font-bold break-words">
          {title || t("list.untitled")}
        </h2>
        <p className="text-n600 flex flex-wrap gap-x-4 gap-y-1 text-sm">
          <span>
            {t("lite.replay.meta", { user: who, count: total })}
            {loaded < total && ` · ${t("lite.replay.loaded", { count: loaded })}`}
          </span>
          {credits != null && (
            <span className="text-ink font-semibold" data-testid="replay-session-credits">
              {t("lite.replay.sessionCredits", { credits: formatCredits(credits) })}
            </span>
          )}
          {failedTurns > 0 && (
            <span className="text-dangerink font-semibold">
              {t("lite.replay.failedTurns", { count: failedTurns })}
            </span>
          )}
        </p>
      </div>
      <Link
        to={proHref}
        className="border-hair hover:bg-hairsoft inline-flex flex-none items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-xs font-medium"
      >
        {t("lite.turn.technicalDetails")}
        <ExternalLink size={12} aria-hidden />
      </Link>
    </header>
  )
}

/** 会话回放: the whole session, oldest turn first, more loaded at the bottom. */
export function TrajectoryReplayPage({ userId, sessionId }: { userId: string; sessionId: string }) {
  const { t } = useTranslation("admin-trajectories")
  const errorMessage = useApiErrorMessage()
  const [search] = useSearchParams()
  const denied = useTrajectoryAccess((s) => s.denied)
  const replay = useSessionReplay(sessionId)
  const labels = useLiteLabels()
  const back = search.get("back") ?? ""
  const target = search.get("turn") ?? ""
  const pages = replay.data?.pages ?? []
  const turns = pages.flatMap((page) => page.items)
  const found = !!target && turns.some((turn) => turn.summary.turn_id === target)
  const scrolled = useRef("")
  // The turn the reader came for starts unfolded; the rest open on request.
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set(target ? [target] : []))
  const toggle = (turnId: string) =>
    setOpened((current) => {
      const next = new Set(current)
      if (!next.delete(turnId)) next.add(turnId)
      return next
    })

  // A turn named in the URL may sit on a later page: read ahead (bounded), then scroll to it once.
  useEffect(() => {
    if (!target || found || !replay.hasNextPage || replay.isFetchingNextPage) return
    if (pages.length >= SEEK_PAGES) return
    void replay.fetchNextPage()
  }, [target, found, replay, pages.length])
  useEffect(() => {
    if (!found || scrolled.current === target) return
    scrolled.current = target
    document.getElementById(`turn-${target}`)?.scrollIntoView({ block: "start" })
  }, [found, target])

  if (denied) return <AccessNotice denial={denied} />
  if (replay.isLoading) {
    return (
      <div
        className="flex items-center justify-center gap-2 py-16"
        data-testid="trajectory-lite-replay-loading"
      >
        <Spinner className="size-5" />
        <span className="sr-only">{t("lite.replay.loading")}</span>
      </div>
    )
  }
  if (replay.error || !pages.length) {
    return (
      <p role="alert" className="text-danger py-10 text-center text-sm">
        {replay.error ? errorMessage(replay.error) : t("lite.turn.stepUnavailable")}
      </p>
    )
  }
  const { session, total_turns: total } = pages[0]!
  const userHref = paths.adminTrajectoryUser(userId, back || undefined)
  const proHref = paths.adminTrajectorySession(sessionId, serializeDetailParams(EMPTY_DETAIL_PARAMS))
  const who = session.username || session.email || session.user_id || userId

  const credits = session.credits

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6" data-testid="trajectory-lite-replay">
      <nav aria-label={t("lite.breadcrumb")} className="text-n500 flex flex-wrap items-center gap-2 text-xs">
        <Link to={paths.adminTrajectories()} className="hover:underline">
          {t("lite.users.title")}
        </Link>
        <span aria-hidden>/</span>
        <Link to={userHref} className="hover:underline">
          {t("lite.turn.backToUser")}
        </Link>
        <span aria-hidden>/</span>
        <span className="text-ink">{t("lite.replay.title")}</span>
        <span className="flex-1" />
        <ModeToggle mode="lite" />
      </nav>

      <SessionHeader
        title={session.title}
        who={who}
        total={total}
        loaded={turns.length}
        credits={credits}
        failedTurns={session.failed_turns ?? 0}
        proHref={proHref}
      />

      {turns.length ? (
        <div className="flex flex-col gap-10">
          {turns.map((turn) => (
            <Turn
              key={turn.summary.turn_id}
              turn={turn}
              userId={userId}
              back={back}
              labels={labels}
              highlighted={turn.summary.turn_id === target}
              open={opened.has(turn.summary.turn_id)}
              onToggle={() => toggle(turn.summary.turn_id)}
            />
          ))}
        </div>
      ) : (
        <p className="text-n500 py-6 text-center text-sm">{t("lite.replay.empty")}</p>
      )}

      {replay.hasNextPage && (
        <div className="flex justify-center">
          <button
            type="button"
            className={BUTTON}
            disabled={replay.isFetchingNextPage}
            onClick={() => void replay.fetchNextPage()}
          >
            {replay.isFetchingNextPage && <Spinner className="size-3" />}
            {t("lite.replay.more", { count: Math.max(0, total - turns.length) })}
          </button>
        </div>
      )}
      {!replay.hasNextPage && turns.length > 0 && (
        <p className="border-hair text-n500 flex flex-wrap justify-between gap-x-4 gap-y-1 border-t pt-4 text-xs">
          <span>{t("lite.replay.end")}</span>
          {credits != null && (
            <span className="text-ink font-semibold">
              {t("lite.replay.sessionCredits", { credits: formatCredits(credits) })}
            </span>
          )}
        </p>
      )}
    </div>
  )
}

// 会话回放 (lite plan §15): one session read top to bottom the way the person
// saw it — their messages, the assistant's replies, questions, files sent — with
// each turn's work folded into one line and every generation prompt in full.
import { lazy, Suspense, useEffect, useRef } from "react"
import { useTranslation } from "react-i18next"
import { ExternalLink, FileText, Paperclip } from "lucide-react"
import { Link, useSearchParams } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { AccessNotice } from "../../components/AccessNotice"
import { useTrajectoryAccess } from "../../stores/access"
import { EMPTY_DETAIL_PARAMS, serializeDetailParams } from "../../utils/params"
import { useLiteLabels, useSessionReplay } from "../api"
import { conclusion, turnStatusTone } from "../labels"
import type { LiteLabels, ReplayEvent, ReplayTurn } from "../types"
import { BUTTON, Clock, Credits, Duration, ToolChips, TurnStatus } from "./bits"
import { PromptCard } from "./MediaPrompts"
import { ModeToggle } from "./ModeToggle"

const MarkdownRenderer = lazy(() => import("../../components/inspector/MarkdownRenderer"))
/** Pages read ahead at most while looking for the turn named in the URL. */
const SEEK_PAGES = 20

const EDGE: Record<string, string> = {
  danger: "bg-dangerink",
  warn: "bg-a700",
  accent: "bg-a700",
  ok: "bg-s800",
  muted: "bg-hair",
}

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

function Event({
  event,
  mainAgentId,
  rawHref,
}: {
  event: ReplayEvent
  mainAgentId: string | null
  rawHref: (recordId: string) => string
}) {
  const { t } = useTranslation("admin-trajectories")
  switch (event.type) {
    case "user":
      return (
        <div className="flex justify-end" data-testid="replay-user">
          <div className="bg-ink text-bg flex max-w-[80%] flex-col gap-1 rounded-2xl rounded-ee-md px-3.5 py-2.5">
            <p className="text-sm leading-6 break-words whitespace-pre-wrap">
              {event.text ?? t("common.dash")}
            </p>
            {event.attachments && (
              <span className="text-2xs inline-flex items-center gap-1 opacity-80">
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
        <div className="flex justify-start" data-testid="replay-reply">
          <div className="border-hair bg-card max-w-[85%] min-w-0 rounded-2xl rounded-es-md border px-3.5 py-2.5 text-sm">
            <Suspense fallback={<Spinner className="size-4" />}>
              <MarkdownRenderer text={event.text} />
            </Suspense>
          </div>
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
    case "media_prompt":
      return (
        <PromptCard
          prompt={event}
          sub={!!mainAgentId && !!event.agent_id && event.agent_id !== mainAgentId}
          rawHref={rawHref(event.record_id)}
          compact
        />
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
}: {
  turn: ReplayTurn
  userId: string
  back: string
  labels: LiteLabels
  highlighted: boolean
}) {
  const { t } = useTranslation("admin-trajectories")
  const { summary } = turn
  const tone = turnStatusTone(summary.status)
  const rawHref = (recordId: string | null) =>
    paths.adminTrajectorySession(
      summary.session_id,
      serializeDetailParams({ ...EMPTY_DETAIL_PARAMS, at: summary.last_seq, record: recordId }),
    )
  const turnHref = paths.adminTrajectoryTurn(
    userId,
    summary.session_id,
    summary.turn_id,
    back ? new URLSearchParams({ back }).toString() : undefined,
  )
  const abnormal = tone === "danger" || tone === "warn"
  const user = turn.events.filter((event) => event.type === "user")
  const rest = turn.events.filter((event) => event.type !== "user")
  return (
    <section
      id={`turn-${summary.turn_id}`}
      className={cn(
        "flex scroll-mt-4 flex-col gap-2.5 rounded-xl p-2",
        highlighted && "bg-a700/5 ring-a700/40 ring-1",
      )}
      data-testid="replay-turn"
    >
      {user.map((event) => (
        <Event key={event.record_id} event={event} mainAgentId={summary.main_agent_id} rawHref={rawHref} />
      ))}
      <Link
        to={turnHref}
        className="border-hair hover:bg-hairsoft text-n700 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border px-3 py-1.5 text-xs"
        data-testid="replay-turn-line"
      >
        <span aria-hidden className={cn("inline-block size-2 flex-none rounded-full", EDGE[tone])} />
        <span className="text-n500">
          <Clock iso={summary.started_at} />
        </span>
        <TurnStatus status={summary.status} reason={summary.status_reason} />
        {abnormal && (
          <span className="text-ink min-w-0 break-words">{conclusion(summary, labels, t, null)}</span>
        )}
        <ToolChips counts={summary.tool_counts} labels={labels} max={4} />
        <span className="flex-1" />
        <Duration ms={summary.duration_ms} />
        <Credits value={summary.credits} input={summary.input_tokens} output={summary.output_tokens} />
        <span className="text-a700">{t("lite.replay.turnDetail")}</span>
      </Link>
      {rest.map((event) => (
        <Event key={event.record_id} event={event} mainAgentId={summary.main_agent_id} rawHref={rawHref} />
      ))}
    </section>
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

  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-lite-replay">
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

      <header className="border-hair bg-card flex flex-wrap items-start justify-between gap-3 rounded-xl border p-4">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="text-ink text-base font-semibold break-words">
            {session.title || t("list.untitled")}
          </h2>
          <p className="text-n500 text-xs">
            {t("lite.replay.meta", { user: who, count: total })}
            {turns.length < total && ` · ${t("lite.replay.loaded", { count: turns.length })}`}
          </p>
        </div>
        <Link
          to={proHref}
          className="bg-ink text-bg inline-flex flex-none items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-medium"
        >
          {t("lite.turn.technicalDetails")}
          <ExternalLink size={12} aria-hidden />
        </Link>
      </header>

      {turns.length ? (
        <div className="flex flex-col gap-3">
          {turns.map((turn) => (
            <Turn
              key={turn.summary.turn_id}
              turn={turn}
              userId={userId}
              back={back}
              labels={labels}
              highlighted={turn.summary.turn_id === target}
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
        <p className="text-n500 text-center text-xs">{t("lite.replay.end")}</p>
      )}
    </div>
  )
}

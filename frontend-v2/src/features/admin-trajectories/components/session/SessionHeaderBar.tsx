import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ArrowLeft } from "lucide-react"
import type { SessionHeader } from "../../types/protocol"
import { CopyButton } from "../inspector/CopyButton"
import { InstantValue } from "../inspector/InstantValue"
import { Status } from "../StatusPills"

interface SessionHeaderBarProps {
  header: SessionHeader
  backHref: string
  /** The position everything below is shown at; null before it is ready. */
  seq: string | null
  live: boolean
  coverageStart: string | null
  /** Header actions, e.g. export. */
  children?: ReactNode
}

interface IdentityProps {
  label: string
  primary: string
  secondary: string | null
}

function Identity({ label, primary, secondary }: IdentityProps) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <div className="flex min-w-0 flex-col">
      <dt className="text-n500 text-2xs">{label}</dt>
      <dd className="flex min-w-0 flex-wrap items-baseline gap-x-2">
        <span className="text-ink truncate text-sm">{primary}</span>
        {secondary && (
          <span className="text-n500 text-2xs flex min-w-0 items-center gap-0.5 font-mono">
            <span className="truncate">{secondary}</span>
            <CopyButton text={secondary} label={t("common.copyValue")} className="size-5" />
          </span>
        )}
      </dd>
    </div>
  )
}

/**
 * Who and what is being watched, always visible: target owner, workspace and
 * session, where recording starts, run and recording states, and the exact
 * position the page is showing. The viewer's own identity never appears here.
 */
export function SessionHeaderBar({
  header,
  backHref,
  seq,
  live,
  coverageStart,
  children,
}: SessionHeaderBarProps) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <header
      className="border-hair bg-card flex flex-col gap-2 rounded-xl border px-4 py-3"
      data-testid="trajectory-header"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Link
          to={backHref}
          className="text-n600 hover:text-ink inline-flex items-center gap-1.5 text-xs"
          data-testid="trajectory-back"
        >
          <ArrowLeft size={13} aria-hidden />
          {t("session.back")}
        </Link>
        <h2 className="text-ink min-w-0 truncate text-base font-medium tracking-tight">
          {header.title || t("list.untitled")}
        </h2>
        <span
          className="text-n500 text-2xs flex items-center gap-0.5 font-mono"
          data-testid="trajectory-session-id"
        >
          {header.session_id}
          <CopyButton text={header.session_id} label={t("common.copyValue")} className="size-5" />
        </span>
        <span className="flex-1" />
        {children}
      </div>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2 xl:grid-cols-[repeat(3,minmax(0,1fr))_auto_auto]">
        <Identity
          label={t("session.owner")}
          primary={header.owner.username ?? header.owner.email ?? header.user_id}
          secondary={header.owner.user_id}
        />
        <Identity
          label={t("session.workspace")}
          primary={header.workspace?.name ?? t("list.noWorkspaceName")}
          secondary={header.workspace_id}
        />
        <div className="flex min-w-0 flex-col">
          <dt className="text-n500 text-2xs">{t("session.coverageStart")}</dt>
          <dd className="text-xs" data-testid="trajectory-coverage-start">
            {coverageStart ? <InstantValue iso={coverageStart} /> : t("list.notStarted")}
          </dd>
        </div>
        <div className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-n500 text-2xs">{t("session.states")}</dt>
          <dd className="flex flex-wrap gap-1.5 whitespace-nowrap">
            <Status scope="run" value={header.running_status} />
            <Status scope="recording" value={header.recording_status} />
          </dd>
        </div>
        <div className="flex min-w-0 flex-col">
          <dt className="text-n500 text-2xs">{t("session.position")}</dt>
          <dd
            className="text-ink text-xs whitespace-nowrap"
            data-testid="trajectory-header-position"
            data-live={live ? "true" : "false"}
          >
            {seq === null
              ? t("session.positionLoading")
              : t(live ? "session.positionLive" : "session.positionReplay", { seq })}
          </dd>
        </div>
      </dl>
    </header>
  )
}

import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { formatDateTime } from "@/shared/lib/format"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { DiagnosticData, MemoryStatus, memoryButton, memoryCard } from "@/shared/ui/MemoryDiagnostics"
import { useDebugRun } from "./api"
import { DebugStep } from "./DebugStep"

const runFields = [
  "request_id",
  "turn_id",
  "attempt_id",
  "project_id",
  "session_id",
  "parent_run_id",
] as const

export function DebugRunDetail({
  id,
  replayAllowed,
  onReplay,
}: {
  id: string
  replayAllowed: boolean
  onReplay: () => void
}) {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const query = useDebugRun(id)
  // A failed read means current access is unknown or gone: never keep showing
  // what an earlier, authorized read returned.
  const detail = query.isError ? undefined : query.data
  const run = detail?.run
  const bodyUnavailable = run?.body_available === false
  return (
    <section className="space-y-4" aria-label={t("debug.detail")}>
      {query.isFetching && <Spinner />}
      {query.error && (
        <p role="alert" className="text-danger text-sm">
          {errorText(query.error)}
        </p>
      )}
      {run && (
        <>
          <div className={`${memoryCard} space-y-3`}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="font-medium">{t("debug.detail")}</h2>
              <MemoryStatus status={run.status} />
            </div>
            <dl className="grid gap-3 text-xs sm:grid-cols-2">
              {runFields.map((key) => (
                <div key={key}>
                  <dt className="text-n500">{t(`debug.${key}`)}</dt>
                  <dd className="mt-1 break-all">{run[key] ?? t("unknown")}</dd>
                </div>
              ))}
              <div>
                <dt className="text-n500">{t("debug.recorded")}</dt>
                <dd className="mt-1">
                  <time dateTime={run.created_at}>{formatDateTime(run.created_at)}</time>
                </dd>
              </div>
              <div>
                <dt className="text-n500">{t("debug.retention")}</dt>
                <dd className="mt-1">{run.expires_at ? formatDateTime(run.expires_at) : t("unknown")}</dd>
              </div>
            </dl>
            <p className="text-n600 text-xs leading-relaxed">{t("debug.redactedHint")}</p>
            {bodyUnavailable && (
              <p className="text-n600 text-sm leading-relaxed" role="status">
                {t("debug.bodyUnavailable")}
                {run?.body_unavailable_reason && <> · {run.body_unavailable_reason}</>}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <button
                className={memoryButton}
                disabled={query.isFetching}
                onClick={() => void query.refetch()}
              >
                {t("refresh")}
              </button>
              <Link className={memoryButton} to={paths.memory}>
                {t("debug.correctLink")}
              </Link>
              <button
                className={memoryButton}
                disabled={!replayAllowed || bodyUnavailable || query.isFetching || !!query.error}
                onClick={onReplay}
              >
                {t("debug.replay")}
              </button>
            </div>
            {!replayAllowed && <p className="text-n500 text-xs">{t("debug.replayDisabled")}</p>}
            <details>
              <summary className="text-n500 cursor-pointer text-xs">{t("debug.runMetadata")}</summary>
              <DiagnosticData data={run} />
            </details>
          </div>
          {detail?.steps.length === 0 && (
            <p className={`${memoryCard} text-n500 text-sm`}>{t("debug.noSteps")}</p>
          )}
          <ol className="space-y-3">
            {detail?.steps.map((step, index) => (
              <DebugStep key={step.id ?? `${step.phase}-${index}`} step={step} order={index + 1} />
            ))}
          </ol>
        </>
      )}
    </section>
  )
}

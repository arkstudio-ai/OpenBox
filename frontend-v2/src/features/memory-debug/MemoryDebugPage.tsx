import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"
import { Activity } from "lucide-react"
import { paths } from "@/shared/router/paths"
import { formatDateTime } from "@/shared/lib/format"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { Spinner } from "@/shared/ui/Spinner"
import { MemoryStatus, memoryButton, memoryCard } from "@/shared/ui/MemoryDiagnostics"
import {
  useDebugHealth,
  useDebugRuns,
  useDebugScope,
  debugCapabilityState,
  type DebugFilters as Filters,
} from "./api"
import { DebugRunDetail } from "./DebugRunDetail"
import { ReplayDialog } from "./ReplayDialog"
import { DebugFilters } from "./DebugFilters"
import { DebugHealth } from "./DebugHealth"

const filterKeys = ["project_id", "session_id", "request_id", "status", "since", "until"] as const
function readFilters(params: URLSearchParams): Filters {
  return Object.fromEntries(filterKeys.map((key) => [key, params.get(key) ?? ""])) as unknown as Filters
}

function DebugWorkspace() {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const navigate = useNavigate()
  const { runId } = useParams<{ runId: string }>()
  const [params, setParams] = useSearchParams()
  const filters = readFilters(params)
  const [cursor, setCursor] = useState<string | null>(null)
  const [replayOpen, setReplayOpen] = useState(false)
  const { key } = useDebugScope()
  const qc = useQueryClient()
  const runs = useDebugRuns(filters, cursor)
  const health = useDebugHealth()
  const capabilities = runs.data?.capabilities ?? health.data?.capabilities
  const { disabled, replayAllowed } = debugCapabilityState(capabilities)
  const apply = (next: Filters) => {
    const query = new URLSearchParams()
    for (const key of filterKeys) if (next[key]) query.set(key, next[key])
    setParams(query)
    setCursor(null)
  }
  return (
    <div className="scr min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto flex max-w-7xl flex-col gap-5">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-medium">
              <Activity size={24} aria-hidden />
              {t("debug.title")}
            </h1>
            <p className="text-n600 mt-2 max-w-3xl text-sm leading-relaxed">{t("debug.subtitle")}</p>
          </div>
          <Link className={memoryButton} to={paths.memory}>
            {t("debug.managementLink")}
          </Link>
        </header>
        <p className="bg-s100 text-s800 rounded-lg px-3 py-2 text-xs" role="status">
          {t("debug.readOnly")}
        </p>
        {disabled && (
          <p className="bg-a100 text-a800 rounded-lg px-3 py-2 text-sm" role="status">
            {t("debug.disabled")}
          </p>
        )}
        <DebugFilters
          key={params.toString()}
          current={filters}
          loading={runs.isFetching || health.isFetching}
          onApply={apply}
          onRefresh={() => {
            void runs.refetch()
            void health.refetch()
          }}
        />
        <DebugHealth capabilities={capabilities} />
        {runs.error && (
          <p className="text-danger text-sm" role="alert">
            {errorText(runs.error)}
          </p>
        )}
        {runs.isLoading && <Spinner />}
        <div
          className={`grid items-start gap-4 ${runId ? "xl:grid-cols-[minmax(16rem,1fr)_minmax(0,2fr)]" : ""}`}
        >
          <section className="space-y-3" aria-label={t("debug.runs")}>
            {runs.data?.runs.length === 0 && (
              <div className={`${memoryCard} text-n500 py-10 text-center text-sm`}>
                {t(disabled ? "debug.noRunsDisabled" : "debug.noRuns")}
              </div>
            )}
            {runs.data?.runs.map((run) => (
              <Link
                key={run.id}
                to={paths.memoryDebugRun(run.id, params.toString())}
                className={`${memoryCard} hover:border-accent block space-y-2 ${runId === run.id ? "border-accent" : ""}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <time className="text-xs" dateTime={run.created_at}>
                    {formatDateTime(run.created_at)}
                  </time>
                  <MemoryStatus status={run.status} />
                </div>
                <p className="text-sm break-all">{run.request_id}</p>
                <p className="text-n500 text-xs break-all">
                  {t("debug.attempt_id")}: {run.attempt_id ?? t("unknown")}
                </p>
                {(run.project_id || run.session_id) && (
                  <p className="text-n500 text-xs break-all">
                    {[run.project_id, run.session_id].filter(Boolean).join(" · ")}
                  </p>
                )}
              </Link>
            ))}
            <div className="flex flex-wrap gap-2">
              {cursor && (
                <button className={memoryButton} onClick={() => setCursor(null)}>
                  {t("debug.firstPage")}
                </button>
              )}
              {runs.data?.next_cursor && (
                <button
                  className={memoryButton}
                  disabled={runs.isFetching}
                  onClick={() => setCursor(runs.data?.next_cursor ?? null)}
                >
                  {t("debug.nextPage")}
                </button>
              )}
            </div>
          </section>
          {runId && (
            <DebugRunDetail
              key={runId}
              id={runId}
              replayAllowed={replayAllowed}
              onReplay={() => setReplayOpen(true)}
            />
          )}
        </div>
        {replayOpen && runId && (
          <ReplayDialog
            key={runId}
            runId={runId}
            onClose={() => setReplayOpen(false)}
            onCreated={(id) => {
              setReplayOpen(false)
              void qc.invalidateQueries({ queryKey: key })
              void navigate(paths.memoryDebugRun(id, params.toString()))
            }}
          />
        )}
      </div>
    </div>
  )
}

export function MemoryDebugPage() {
  const { userId, workspaceId } = useDebugScope()
  return <DebugWorkspace key={`${userId}:${workspaceId}`} />
}

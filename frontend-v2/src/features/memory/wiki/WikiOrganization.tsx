import { useState } from "react"
import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
import { MemoryStatus, memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { platformApi, type Maintenance, type OrganizationRun } from "./platform-api"
import { CostConsent, Field, panel, ScopeHint, useWikiAction, WikiError } from "./platform-ui"

export function WikiOrganization({ projectId, enabled }: { projectId: string; enabled: boolean }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const preview = useQuery({
    queryKey: [...key, "wiki-organization-preview", projectId],
    queryFn: () => platformApi.preview(projectId),
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: 10000,
  })
  const policy = useQuery({
    queryKey: [...key, "wiki-maintenance", projectId],
    queryFn: () => platformApi.maintenance(projectId),
    refetchInterval: 10000,
  })
  const runs = useInfiniteQuery({
    queryKey: [...key, "wiki-organization-runs", projectId],
    queryFn: ({ pageParam }) => platformApi.runs(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 2000,
  })
  const [budget, setBudget] = useState(20)
  const [compile, setCompile] = useState(true)
  const [consent, setConsent] = useState(false)
  const [confirmedHash, setConfirmedHash] = useState("")
  const start = useWikiAction(
    () => platformApi.organize(projectId, preview.data!, budget, compile),
    () => setConsent(false),
  )
  return (
    <section className="space-y-5" aria-label={t("organize")}>
      <div className={panel}>
        <h2 className="text-xl font-semibold">{t("organize")}</h2>
        <p className="text-n500 text-sm leading-6">{t("organizeHint")}</p>
        <ScopeHint projectId={projectId} />
        <WikiError error={preview.error ?? policy.error ?? start.error ?? runs.error} />
        {preview.data && (
          <>
            <p className="text-sm">
              {t("organizationPreview", {
                total: preview.data.memory_count,
                changed: preview.data.changed_count,
                reused: preview.data.reused_count,
              })}
            </p>
            {!!preview.data.skipped.length && (
              <p className="text-danger text-sm">
                {t("skippedSources", { count: preview.data.skipped.length })}
              </p>
            )}
            <p className="text-n500 text-xs">{t("model", { model: preview.data.model })}</p>
          </>
        )}
        <Field label={t("callBudget")}>
          <input
            type="number"
            min={1}
            max={preview.data?.max_model_calls ?? 400}
            className={memoryInput + " max-w-40"}
            value={budget}
            onChange={(event) => {
              setBudget(Number(event.target.value))
              setConsent(false)
            }}
          />
        </Field>
        <label className="flex gap-2 text-sm">
          <input
            type="checkbox"
            checked={compile}
            onChange={(event) => {
              setCompile(event.target.checked)
              setConsent(false)
            }}
          />
          {t("compileConceptPages")}
        </label>
        <CostConsent
          value={consent && confirmedHash === preview.data?.input_hash}
          onChange={(value) => {
            setConsent(value)
            setConfirmedHash(preview.data?.input_hash ?? "")
          }}
        />
        <div className="flex flex-wrap gap-2">
          <button
            className={memoryPrimary}
            disabled={
              !enabled ||
              !consent ||
              confirmedHash !== preview.data?.input_hash ||
              !preview.data ||
              budget < 1 ||
              start.isPending
            }
            onClick={() => start.mutate()}
          >
            {t("startOrganize")}
          </button>
          <button
            className={memoryButton}
            onClick={() => {
              setConsent(false)
              void preview.refetch()
            }}
          >
            {t("refreshPreview")}
          </button>
        </div>
      </div>
      {policy.data && (
        <MaintenanceSettings
          key={policy.data.revision}
          projectId={projectId}
          policy={policy.data}
          enabled={enabled}
        />
      )}
      <h3 className="text-lg font-semibold">{t("organizationRuns")}</h3>
      {!runs.data?.pages.some((page) => page.runs.length) && (
        <p className="text-n500 text-sm">{t("noRuns")}</p>
      )}
      {runs.data?.pages
        .flatMap((page) => page.runs)
        .map((run) => (
          <OrganizationRunCard key={run.id} run={run} />
        ))}
      {runs.hasNextPage && (
        <button
          className={memoryButton}
          disabled={runs.isFetchingNextPage}
          onClick={() => void runs.fetchNextPage()}
        >
          {t("loadMore")}
        </button>
      )}
    </section>
  )
}

export function OrganizationRunCard({ run }: { run: OrganizationRun }) {
  const { t } = useTranslation("wiki")
  const [budget, setBudget] = useState(Math.max(run.max_model_calls, run.model_calls + 10))
  const [consent, setConsent] = useState(false)
  const act = useWikiAction(
    (action: "cancel" | "resume") =>
      platformApi.actRun(run, action, action === "resume" ? budget : undefined),
    () => setConsent(false),
  )
  const pending = ["pending", "running", "retry", "paused"].includes(run.status)
  return (
    <article className={panel}>
      <div className="flex flex-wrap items-center gap-3">
        <MemoryStatus status={run.status} />
        <span className="text-sm">{t("phases." + run.phase)}</span>
        <span className="text-n500 ms-auto text-xs">{new Date(run.created_at).toLocaleString()}</span>
      </div>
      <p className="text-sm">
        {t("organizationProgress", {
          processed: run.processed,
          total: run.memory_count,
          concepts: run.concept_count,
          calls: run.model_calls,
          max: run.max_model_calls,
        })}
      </p>
      <progress
        aria-label={t("organizationRuns")}
        className="w-full"
        value={run.processed}
        max={Math.max(1, run.memory_count)}
      />
      {run.reason_code && (
        <p className="text-n600 text-sm">
          {t("platformErrors." + run.reason_code, { defaultValue: t("runNeedsAttention") })}
        </p>
      )}
      {run.pages.some((page) => page.status === "needs_selection") && (
        <p className="text-danger text-sm">{t("needsSelectionHint")}</p>
      )}
      {!!run.skipped.length && (
        <p className="text-danger text-sm">{t("skippedSources", { count: run.skipped.length })}</p>
      )}
      <WikiError error={act.error} />
      {!!run.pages.length && (
        <Link
          className="text-a700 text-sm underline"
          to={paths.wiki(run.project_id ?? "") + (run.project_id ? "&" : "?") + "view=reviews"}
        >
          {t("openReviews")}
        </Link>
      )}
      {run.can_resume && (
        <div className="space-y-3">
          <Field label={t("newTotalBudget")}>
            <input
              className={memoryInput}
              type="number"
              min={run.model_calls + 1}
              value={budget}
              onChange={(event) => {
                setBudget(Number(event.target.value))
                setConsent(false)
              }}
            />
          </Field>
          <CostConsent value={consent} onChange={setConsent} />
          <button
            className={memoryPrimary}
            disabled={!consent || act.isPending}
            onClick={() => act.mutate("resume")}
          >
            {t("resumeRun")}
          </button>
        </div>
      )}
      {pending && (
        <button className={memoryButton} disabled={act.isPending} onClick={() => act.mutate("cancel")}>
          {t("cancelRun")}
        </button>
      )}
    </article>
  )
}

function MaintenanceSettings({
  projectId,
  policy,
  enabled,
}: {
  projectId: string
  policy: Maintenance
  enabled: boolean
}) {
  const { t } = useTranslation("wiki")
  const [limit, setLimit] = useState(policy.call_limit)
  const [compile, setCompile] = useState(policy.compile_pages)
  const [consent, setConsent] = useState(false)
  const configure = useWikiAction(
    (active: boolean) =>
      platformApi.configure(projectId, policy, {
        enabled: active,
        call_limit: limit,
        compile_pages: compile,
      }),
    () => setConsent(false),
  )
  return (
    <details className={panel} open={policy.enabled}>
      <summary className="cursor-pointer font-semibold">
        {t("maintenance")} · {t(policy.enabled ? "enabled" : "disabledShort")}
      </summary>
      <p className="text-n500 text-sm leading-6">{t("maintenanceHint")}</p>
      <p className="text-sm">
        {t("maintenanceUsage", { calls: policy.calls_used, limit: policy.call_limit })}
      </p>
      <Field label={t("dailyBudget")}>
        <input
          className={memoryInput + " max-w-40"}
          type="number"
          min={1}
          value={limit}
          onChange={(event) => {
            setLimit(Number(event.target.value))
            setConsent(false)
          }}
        />
      </Field>
      <label className="flex gap-2 text-sm">
        <input
          type="checkbox"
          checked={compile}
          onChange={(event) => {
            setCompile(event.target.checked)
            setConsent(false)
          }}
        />
        {t("compileConceptPages")}
      </label>
      <CostConsent value={consent} onChange={setConsent} />
      <WikiError error={configure.error} />
      <div className="flex flex-wrap gap-2">
        <button
          className={memoryPrimary}
          disabled={!enabled || !consent || configure.isPending || limit < 1}
          onClick={() => configure.mutate(true)}
        >
          {t(policy.enabled ? "saveMaintenance" : "enableMaintenance")}
        </button>
        {policy.enabled && (
          <button
            className={memoryButton}
            disabled={configure.isPending}
            onClick={() => configure.mutate(false)}
          >
            {t("disableMaintenance")}
          </button>
        )}
      </div>
    </details>
  )
}

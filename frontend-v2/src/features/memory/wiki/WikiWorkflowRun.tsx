import { useState } from "react"
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
import { MemoryStatus, memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { wikiApi } from "../wiki-api"
import { platformApi } from "./platform-api"
import { workflowApi, type StageDefinition, type WorkflowRun } from "./workflow-api"
import { CostConsent, Field, panel, saveDownload, useWikiAction, WikiError } from "./platform-ui"
import { WikiWorkflowOutput } from "./WikiWorkflowOutput"

export function WikiWorkflowRun({ runId }: { runId: string }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const [offset, setOffset] = useState(0)
  const queryKey = [...key, "wiki-workflow", runId, offset]
  const query = useQuery({ queryKey, queryFn: () => workflowApi.run(runId, offset), refetchInterval: 2000 })
  const run = query.data
  const action = useWikiAction(
    (value: { action: string; payload?: Record<string, unknown> }) =>
      workflowApi.act(run!, value.action, value.payload),
    (result) =>
      qc.setQueryData(queryKey, (previous: WorkflowRun | undefined) => ({ ...previous, ...result })),
  )
  const history = useWikiAction(() => workflowApi.history(runId), saveDownload)
  if (!run || query.error)
    return (
      <div className={panel}>
        <WikiError error={query.error} />
        <p>{t("loading")}</p>
      </div>
    )
  const stages = run.definition.workflows[run.workflow_id].stages
  const stage = stages[run.stage_index]
  const state = stage ? run.stages[stage.id] : undefined
  const terminal = ["completed", "cancelled"].includes(run.status)
  return (
    <article className={panel}>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xl font-semibold">
          {run.definition.workflows[run.workflow_id].title ?? run.workflow_id}
        </h2>
        <MemoryStatus status={run.status} />
        <span className="text-n500 text-xs">{t("revision", { revision: run.revision })}</span>
      </div>
      <ol className="flex flex-wrap gap-3">
        {stages.map((item, index) => (
          <li
            key={item.id}
            className={
              (index === run.stage_index ? "border-accent bg-a100" : "border-hair") +
              " rounded-lg border p-3 text-sm"
            }
          >
            <span className="text-n500 me-2">{index + 1}</span>
            {item.title ?? item.id}
            <span className="mt-2 block">
              <MemoryStatus status={run.stages[item.id]?.status ?? "pending"} />
            </span>
          </li>
        ))}
      </ol>
      <WikiError error={action.error ?? history.error} />
      {run.reason && <p className="text-n500 text-sm">{run.reason}</p>}
      {run.definition_changed && !terminal && (
        <AdaptWorkflow
          run={run}
          pending={action.isPending}
          onAdapt={(hash) => action.mutate({ action: "adapt", payload: { preview_hash: hash } })}
        />
      )}
      {!run.definition_changed && !terminal && stage && state && (
        <>
          {stage.action && (
            <PrepareWorkflow
              key={stage.id}
              run={run}
              stage={stage}
              pending={action.isPending}
              onAction={(next, payload) => action.mutate({ action: next, payload })}
            />
          )}
          {run.status === "running" && (
            <>
              <WikiWorkflowOutput
                key={stage.id}
                run={run}
                stage={stage}
                pending={action.isPending}
                onSubmit={(outputs) => action.mutate({ action: "submit", payload: { outputs } })}
              />
              <div className="border-hair space-y-3 border-t pt-4">
                <h3 className="font-semibold">{t("stageReview")}</h3>
                <p className="text-n500 text-sm">{t("approvalBindingHint")}</p>
                {!state.outputs_available && <p className="text-danger text-sm">{t("staleHint")}</p>}
                {state.outputs_available && !!state.outputs.length && (
                  <pre className="bg-rail max-h-72 overflow-auto rounded-lg p-3 text-xs break-words whitespace-pre-wrap">
                    {JSON.stringify(state.outputs, null, 2)}
                  </pre>
                )}
                <div className="flex flex-wrap gap-2">
                  {stage.gates
                    .filter((gate) => gate.startsWith("human:"))
                    .map((gate) => (
                      <button
                        key={gate}
                        className={memoryButton}
                        disabled={action.isPending || !!state.approvals[gate] || !state.outputs_available}
                        onClick={() =>
                          action.mutate({
                            action: "approve",
                            payload: { gate, output_hash: state.output_hash },
                          })
                        }
                      >
                        {state.approvals[gate] ? t("gateApproved") : t("approveGate")} · {gate.split(":")[1]}
                      </button>
                    ))}
                  <button
                    className={memoryPrimary}
                    disabled={action.isPending || !state.outputs_available}
                    onClick={() => action.mutate({ action: "advance" })}
                  >
                    {t(run.stage_index === stages.length - 1 ? "completeWorkflow" : "advanceStage")}
                  </button>
                </div>
              </div>
            </>
          )}
          {run.status === "failed" && (
            <button
              className={memoryPrimary}
              disabled={action.isPending}
              onClick={() => action.mutate({ action: "resume" })}
            >
              {t("resumeRun")}
            </button>
          )}
        </>
      )}
      {!terminal && (
        <WorkflowControls run={run} pending={action.isPending} onAction={(value) => action.mutate(value)} />
      )}
      <WorkflowResults run={run} />

      <div className="border-hair space-y-3 border-t pt-4">
        <div className="flex flex-wrap justify-between gap-2">
          <h3 className="font-semibold">{t("workflowEvents")}</h3>
          <button className={memoryButton} disabled={history.isPending} onClick={() => history.mutate()}>
            {t("exportHistory")}
          </button>
        </div>
        <ol className="space-y-2">
          {run.events?.map((event) => (
            <li key={event.id} className="flex flex-wrap gap-2 text-xs">
              <span className="text-n500">
                {event.version} · {new Date(event.created_at).toLocaleString()}
              </span>
              <span>{t("workflowActions." + event.action)}</span>
              <span>{event.stage_id}</span>
            </li>
          ))}
        </ol>
        <div className="flex gap-2">
          {offset > 0 && (
            <button className={memoryButton} onClick={() => setOffset(Math.max(0, offset - 100))}>
              {t("previousEvents")}
            </button>
          )}
          {run.events_next_offset != null && (
            <button className={memoryButton} onClick={() => setOffset(run.events_next_offset!)}>
              {t("loadMore")}
            </button>
          )}
        </div>
      </div>
    </article>
  )
}

function PrepareWorkflow({
  run,
  stage,
  pending,
  onAction,
}: {
  run: WorkflowRun
  stage: StageDefinition
  pending: boolean
  onAction: (action: string, payload: Record<string, unknown>) => void
}) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const [consentHash, setConsentHash] = useState<string | null>(null)
  const [budget, setBudget] = useState(20)
  const [title, setTitle] = useState("")
  const [slug, setSlug] = useState("")
  const [selected, setSelected] = useState<string[]>([])
  const task = run.stages[stage.id].task
  const preview = useQuery({
    queryKey: [...key, "wiki-organization-preview", run.project_id],
    queryFn: () => platformApi.preview(run.project_id ?? ""),
    enabled: stage.action === "organize",
    staleTime: 0,
    refetchOnMount: "always",
  })
  const sources = useInfiniteQuery({
    queryKey: [...key, "wiki-task-sources", run.project_id],
    queryFn: ({ pageParam }) => wikiApi.compileSources(run.project_id ?? "", pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    enabled: stage.action === "compile",
  })
  const snapshotHash = JSON.stringify([preview.data?.input_hash, budget, title, slug, selected])
  const consent = consentHash === snapshotHash
  const setConsent = (value: boolean) => setConsentHash(value ? snapshotHash : null)
  const retryable = task && ["failed", "paused", "partial"].includes(task.status)
  const submit = () => {
    onAction(task ? "retry_task" : "prepare", {
      confirm_cost: true,
      max_model_calls: budget,
      input_hash: preview.data?.input_hash,
      title,
      slug,
      memory_ids: selected,
    })
    setConsent(false)
  }
  return (
    <section className="border-hair space-y-4 border-t pt-4">
      <h3 className="font-semibold">{t("workflowTask")}</h3>
      <WikiError error={preview.error ?? sources.error} />
      {task && (
        <>
          <div className="flex items-center gap-3">
            <MemoryStatus status={task.status} />
            <span className="text-n500 text-sm">{t("taskCalls", { calls: task.model_calls ?? 0 })}</span>
          </div>
          <Link
            className="text-a700 text-sm underline"
            to={paths.wiki(run.project_id ?? "") + (run.project_id ? "&" : "?") + "view=reviews"}
          >
            {t("openReviews")}
          </Link>
        </>
      )}
      {(!task || retryable) && (
        <>
          <Field label={t(task ? "newTotalBudget" : "callBudget")}>
            <input
              type="number"
              min={1}
              className={memoryInput + " max-w-40"}
              value={budget}
              onChange={(event) => {
                setBudget(Number(event.target.value))
                setConsent(false)
              }}
            />
          </Field>
          {stage.action === "organize" && preview.data && (
            <p className="text-n500 text-sm">
              {t("organizationPreview", {
                total: preview.data.memory_count,
                changed: preview.data.changed_count,
                reused: preview.data.reused_count,
              })}
            </p>
          )}
          {stage.action === "compile" && !task && (
            <>
              <Field label={t("pageTitle")}>
                <input
                  className={memoryInput}
                  value={title}
                  onChange={(event) => {
                    setTitle(event.target.value)
                    setConsent(false)
                  }}
                />
              </Field>
              <Field label={t("pageAddress")}>
                <input
                  className={memoryInput}
                  value={slug}
                  onChange={(event) => {
                    setSlug(event.target.value)
                    setConsent(false)
                  }}
                />
              </Field>
              {sources.data?.pages
                .flatMap((batch) => batch.memories)
                .map((memory) => (
                  <label key={memory.id} className="flex gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={selected.includes(memory.id)}
                      onChange={(event) => {
                        setSelected(
                          event.target.checked
                            ? [...selected, memory.id]
                            : selected.filter((id) => id !== memory.id),
                        )
                        setConsent(false)
                      }}
                    />
                    {memory.summary}
                  </label>
                ))}
              {sources.hasNextPage && (
                <button className={memoryButton} onClick={() => void sources.fetchNextPage()}>
                  {t("loadMore")}
                </button>
              )}
            </>
          )}
          <CostConsent value={consent} onChange={setConsent} />
          <button
            className={memoryPrimary}
            disabled={!consent || pending || run.status !== "running"}
            onClick={submit}
          >
            {t(task ? "retryTask" : "startTask")}
          </button>
        </>
      )}
    </section>
  )
}

function WorkflowResults({ run }: { run: WorkflowRun }) {
  const { t } = useTranslation("wiki")
  const download = useWikiAction((id: string) => workflowApi.artifact(id), saveDownload)
  const completed = Object.entries(run.stages).filter(
    ([, state]) => state.status === "completed" && state.results.length,
  )
  if (!completed.length) return null
  return (
    <section className="border-hair space-y-3 border-t pt-4">
      <h3 className="font-semibold">{t("completedOutputs")}</h3>
      <WikiError error={download.error} />
      {completed.map(([id, state]) => (
        <details key={id}>
          <summary className="cursor-pointer text-sm">
            {id} · {state.results.length}
          </summary>
          <pre className="bg-rail mt-2 max-h-60 overflow-auto rounded-lg p-3 text-xs break-words whitespace-pre-wrap">
            {JSON.stringify(state.results, null, 2)}
          </pre>
          {state.results
            .filter((result) => typeof result.ref === "string" && typeof result.id === "string")
            .map((result) => (
              <button
                key={String(result.id)}
                className={memoryButton + " mt-2"}
                disabled={download.isPending}
                onClick={() => download.mutate(String(result.id))}
              >
                {t("downloadArtifact")}
              </button>
            ))}
        </details>
      ))}
    </section>
  )
}

function AdaptWorkflow({
  run,
  pending,
  onAdapt,
}: {
  run: WorkflowRun
  pending: boolean
  onAdapt: (hash: string) => void
}) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const [reviewed, setReviewed] = useState(false)
  const preview = useQuery({
    queryKey: [...key, "wiki-workflow-adaptation", run.id, run.revision],
    queryFn: () => workflowApi.adaptation(run.id),
  })
  return (
    <div className="border-danger space-y-3 rounded-xl border p-4">
      <h3 className="font-semibold">{t("definitionChanged")}</h3>
      <p className="text-sm">{t("adaptationHint")}</p>
      <WikiError error={preview.error} />
      {preview.data && (
        <>
          <details>
            <summary className="cursor-pointer text-sm">{t("compareDefinitions")}</summary>
            <div className="mt-3 grid gap-3 xl:grid-cols-2">
              <pre className="max-h-80 overflow-auto text-xs whitespace-pre-wrap">
                {JSON.stringify(run.definition, null, 2)}
              </pre>
              <pre className="max-h-80 overflow-auto text-xs whitespace-pre-wrap">
                {JSON.stringify(preview.data.new_definition, null, 2)}
              </pre>
            </div>
          </details>
          {preview.data.allowed ? (
            <>
              <label className="flex gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={reviewed}
                  onChange={(event) => setReviewed(event.target.checked)}
                />
                {t("approveAdaptation")}
              </label>
              <button
                className={memoryPrimary}
                disabled={!reviewed || pending}
                onClick={() => onAdapt(preview.data!.preview_hash)}
              >
                {t("applyAdaptation")}
              </button>
            </>
          ) : (
            <p className="text-danger text-sm">{t("adaptationBlocked")}</p>
          )}
        </>
      )}
    </div>
  )
}

function WorkflowControls({
  run,
  pending,
  onAction,
}: {
  run: WorkflowRun
  pending: boolean
  onAction: (value: { action: string; payload?: Record<string, unknown> }) => void
}) {
  const { t } = useTranslation("wiki")
  const [reason, setReason] = useState("")
  return (
    <details>
      <summary className="cursor-pointer text-sm">{t("workflowControls")}</summary>
      <div className="mt-3 space-y-3">
        <Field label={t("failureReason")}>
          <input
            className={memoryInput}
            value={reason}
            maxLength={800}
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
        <div className="flex flex-wrap gap-2">
          <button
            className={memoryButton}
            disabled={!reason.trim() || pending || run.status !== "running" || run.definition_changed}
            onClick={() => onAction({ action: "fail", payload: { reason } })}
          >
            {t("failRun")}
          </button>
          <button className={memoryButton} disabled={pending} onClick={() => onAction({ action: "cancel" })}>
            {t("cancelRun")}
          </button>
        </div>
      </div>
    </details>
  )
}

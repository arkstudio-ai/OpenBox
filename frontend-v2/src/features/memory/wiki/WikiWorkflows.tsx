import { useState } from "react"
import { useInfiniteQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { MemoryStatus, memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { workflowApi, type Profile } from "./workflow-api"
import { Field, panel, useWikiAction, WikiError } from "./platform-ui"
import { WikiProfiles } from "./WikiProfiles"
import { WikiFields } from "./WikiFields"
import { WikiWorkflowRun } from "./WikiWorkflowRun"

export function WikiWorkflows({ projectId }: { projectId: string }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const [profilesView, setProfilesView] = useState(false)
  const [runId, setRunId] = useState("")
  const profiles = useInfiniteQuery({
    queryKey: [...key, "wiki-profiles", projectId],
    queryFn: ({ pageParam }) => workflowApi.profiles(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  })
  const runs = useInfiniteQuery({
    queryKey: [...key, "wiki-workflows", projectId],
    queryFn: ({ pageParam }) => workflowApi.runs(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 5000,
  })
  const allProfiles = profiles.data?.pages.flatMap((page) => page.profiles) ?? []
  return (
    <section className="space-y-5" aria-label={t("workflows")}>
      <div className="flex flex-wrap gap-2">
        <button
          aria-pressed={!profilesView}
          className={!profilesView ? memoryPrimary : memoryButton}
          onClick={() => setProfilesView(false)}
        >
          {t("workflows")}
        </button>
        <button
          aria-pressed={profilesView}
          className={profilesView ? memoryPrimary : memoryButton}
          onClick={() => setProfilesView(true)}
        >
          {t("profiles")}
        </button>
      </div>
      <WikiError error={profiles.error ?? runs.error} />
      {profiles.hasNextPage && (
        <button className={memoryButton} onClick={() => void profiles.fetchNextPage()}>
          {t("moreProfiles")}
        </button>
      )}
      {profilesView ? (
        <WikiProfiles projectId={projectId} profiles={allProfiles} />
      ) : (
        <>
          {!runId && (
            <>
              <StartWorkflow profiles={allProfiles} onStart={setRunId} />
              <div className={panel}>
                <h3 className="font-semibold">{t("workflowRuns")}</h3>
                {!runs.data?.pages.some((page) => page.runs.length) && (
                  <p className="text-n500 text-sm">{t("noRuns")}</p>
                )}
                {runs.data?.pages
                  .flatMap((page) => page.runs)
                  .map((run) => (
                    <button
                      key={run.id}
                      className="border-hair flex w-full flex-wrap items-center gap-3 border-t py-3 text-start text-sm"
                      onClick={() => setRunId(run.id)}
                    >
                      <span className="text-a700 font-medium">{run.workflow_id}</span>
                      <MemoryStatus status={run.status} />
                      {run.status !== "completed" && (
                        <span className="text-n500 ms-auto">
                          {t("stageNumber", { number: run.stage_index + 1 })}
                        </span>
                      )}
                    </button>
                  ))}
                {runs.hasNextPage && (
                  <button className={memoryButton} onClick={() => void runs.fetchNextPage()}>
                    {t("loadMore")}
                  </button>
                )}
              </div>
            </>
          )}
          {runId && (
            <>
              <button className={memoryButton} onClick={() => setRunId("")}>
                {t("backToRuns")}
              </button>
              <WikiWorkflowRun key={runId} runId={runId} />
            </>
          )}
        </>
      )}
    </section>
  )
}

function StartWorkflow({ profiles, onStart }: { profiles: Profile[]; onStart: (id: string) => void }) {
  const { t } = useTranslation("wiki")
  const [profileId, setProfileId] = useState("")
  const [workflowId, setWorkflowId] = useState("")
  const [values, setValues] = useState<Record<string, unknown>>({})
  const profile = profiles.find((item) => item.id === profileId)
  const workflow = profile?.definition.workflows[workflowId]
  const start = useWikiAction(
    () => workflowApi.start(profile!, workflowId, values),
    (result) => onStart(result.id),
  )
  return (
    <form
      className={panel}
      onSubmit={(event) => {
        event.preventDefault()
        start.mutate()
      }}
    >
      <h2 className="text-xl font-semibold">{t("startWorkflow")}</h2>
      <p className="text-n500 text-sm leading-6">{t("workflowHint")}</p>
      {!profiles.length && <p className="text-n500 text-sm">{t("noProfiles")}</p>}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("chooseProfile")}>
          <select
            required
            className={memoryInput}
            value={profileId}
            onChange={(event) => {
              setProfileId(event.target.value)
              setWorkflowId("")
              setValues({})
            }}
          >
            <option value="">{t("choose")}</option>
            {profiles.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("chooseWorkflow")}>
          <select
            required
            className={memoryInput}
            value={workflowId}
            onChange={(event) => {
              setWorkflowId(event.target.value)
              setValues({})
            }}
          >
            <option value="">{t("choose")}</option>
            {Object.entries(profile?.definition.workflows ?? {}).map(([id, item]) => (
              <option key={id} value={id}>
                {item.title ?? id}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {workflow && <WikiFields definitions={workflow.inputs ?? {}} values={values} onChange={setValues} />}
      <WikiError error={start.error} />
      <button className={memoryPrimary} disabled={!workflow || start.isPending}>
        {t("createWorkflowRun")}
      </button>
    </form>
  )
}

import { useState, type ReactNode } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { ArrowLeft, X } from "lucide-react"
import type { ModelInfo } from "@/shared/types/api"
import { paths } from "@/shared/router/paths"
import { WorkspaceActions } from "@/shared/ui/WorkspaceActions"
import { Spinner } from "@/shared/ui/Spinner"
import { cn } from "@/shared/lib/cn"
import { useAgentTrial, useTeamPreview, useAgentDraftGeneration } from "../api/teams"
import type { AgentSpec, CapabilitySummary, Definition, TeamSpec } from "../types"
import { useDefinitionDraft } from "../hooks/useDefinitionDraft"
import { useSavedDefinitionUrl } from "../hooks/useSavedDefinitionUrl"
import { emptyAgent, emptyTeam, validAgentDraft, validTeamDraft } from "../lib/defaults"
import { AgentForm } from "./AgentForm"
import { TeamForm } from "./TeamForm"
import { BUTTON, PRIMARY } from "./FormFields"

function CapabilityIssues({ summary }: { summary?: CapabilitySummary }) {
  return (
    <>
      {summary?.issues?.map((issue) => (
        <p key={issue.code} className="text-danger text-xs">
          {issue.message}
        </p>
      ))}
      {summary?.warnings?.map((issue, index) => (
        <p key={index} className="text-a700 text-xs">
          {issue.message ?? issue.code}
          {issue.tools?.length ? ` · ${issue.tools.join(", ")}` : ""}
        </p>
      ))}
    </>
  )
}

function SaveState({
  saved,
  busy,
  version,
  published,
}: {
  saved: boolean
  busy: boolean
  version?: number | null
  published: boolean
}) {
  const { t } = useTranslation("teams")
  return (
    <span className="text-n600 hidden text-xs lg:block">
      {t(busy ? "saving" : published ? "published" : saved ? "autosaved" : "unsaved")}
      {version && ` · ${t(published ? "publishedVersion" : "liveVersion", { version })}`}
    </span>
  )
}

function TrialPane({
  mobile,
  onClose,
  disabled,
  readonly,
  pending,
  trialId,
  onStart,
  spec,
  children,
}: {
  mobile: boolean
  onClose: () => void
  disabled: boolean
  readonly: boolean
  pending: boolean
  trialId: string | null
  onStart: () => void
  spec: AgentSpec
  children: ReactNode
}) {
  const { t } = useTranslation("teams")
  return (
    <div
      className={cn(
        "border-hair bg-bg min-h-0 flex-1 flex-col border-s lg:flex",
        mobile ? "fixed inset-0 z-40 flex lg:static lg:z-auto" : "hidden",
      )}
    >
      <div className="border-hair flex items-center gap-3 border-b px-4 py-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{t("try")}</p>
          <p className="text-n600 truncate text-xs">
            {t("trialSummary", { skills: spec.skill_refs.length, tools: spec.tool_allowlist.length })}
          </p>
        </div>
        {!readonly && (
          <button type="button" className={BUTTON} disabled={disabled} onClick={onStart}>
            {t(trialId ? "restart" : "startTrial")}
          </button>
        )}
        <button
          type="button"
          className="rounded-full p-2 lg:hidden"
          aria-label={t("close")}
          onClick={onClose}
        >
          <X className="size-4" />
        </button>
      </div>
      {pending ? (
        <div className="flex flex-1 items-center justify-center">
          <Spinner className="size-5" />
        </div>
      ) : trialId ? (
        <div className="min-h-0 flex-1">{children}</div>
      ) : (
        <div className="text-n600 flex flex-1 items-center justify-center p-8 text-center text-sm">
          {t("trialHint")}
        </div>
      )}
    </div>
  )
}

export function AgentEditor({
  definition,
  models,
  renderChat,
  projectId,
  initialSpec,
}: {
  definition?: Definition<AgentSpec>
  models: ModelInfo[]
  renderChat: (sessionId: string) => ReactNode
  projectId?: string
  initialSpec?: AgentSpec
}) {
  const { t } = useTranslation("teams")
  const draft = useDefinitionDraft(
    "agent",
    definition?.version.spec ?? initialSpec ?? emptyAgent(),
    definition,
    validAgentDraft,
  )
  const [trialId, setTrialId] = useState<string | null>(null)
  const [mobileTrial, setMobileTrial] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const trial = useAgentTrial()
  const generate = useAgentDraftGeneration()
  const readonly = isReadonly(definition)
  useSavedDefinitionUrl("agent", draft.record?.id, draft.saved)
  const valid = validAgentDraft(draft.spec)
  const startTrial = async () => {
    try {
      const record = await draft.save()
      const result = await trial.mutateAsync({ id: record.id, versionId: record.version.id, projectId })
      setTrialId(result.session_id)
      setMobileTrial(true)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    }
  }
  const publish = async () => {
    try {
      await draft.publish()
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    }
  }
  const optimize = async (field: "instruction" | "when_to_use") => {
    try {
      const result = await generate.mutateAsync({ prompt: t("optimizePrompt"), spec: draft.spec, field })
      draft.setSpec((current) => ({ ...current, [field]: result.spec[field] }))
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    }
  }
  return (
    <div className="flex min-h-0 flex-1">
      <WorkspaceActions>
        <SaveState
          published={draft.published}
          busy={draft.pending}
          saved={draft.saved}
          version={draft.record?.current_version_number}
        />
        {!readonly && (
          <button
            type="button"
            className={PRIMARY}
            disabled={!valid || draft.pending || draft.published}
            onClick={() => void publish()}
          >
            {t(draft.published ? "published" : "publishEnable")}
          </button>
        )}
      </WorkspaceActions>
      <div className="scr min-h-0 flex-1 overflow-auto px-5 pb-8 lg:max-w-1/2">
        <Link to={paths.agents} className="text-n600 mb-4 inline-flex items-center gap-1 text-xs">
          <ArrowLeft className="size-3.5" />
          {t("backLibrary")}
        </Link>
        <h1 className="mb-4 text-xl font-medium">{draft.record?.name ?? t("newAgent")}</h1>
        {readonly && <p className="bg-hairsoft mb-4 rounded-lg p-3 text-xs">{t("readonlyDefinition")}</p>}
        {(error ?? draft.error) && (
          <p role="alert" className="text-danger mb-3 text-sm">
            {(error ?? draft.error)?.message}
          </p>
        )}
        <CapabilityIssues summary={draft.record?.version.capability_summary} />
        <fieldset disabled={!!readonly || generate.isPending} className="min-w-0">
          <AgentForm
            spec={draft.spec}
            onChange={draft.setSpec}
            models={models}
            onOptimize={(field) => void optimize(field)}
          />
        </fieldset>
        {!readonly && (
          <button
            type="button"
            className={`${PRIMARY} mt-4 lg:hidden`}
            disabled={!valid || draft.pending || trial.isPending}
            onClick={() => (trialId ? setMobileTrial(true) : void startTrial())}
          >
            {t("try")}
          </button>
        )}
      </div>
      <TrialPane
        mobile={mobileTrial}
        onClose={() => setMobileTrial(false)}
        readonly={!!readonly}
        disabled={!valid || draft.pending || trial.isPending}
        pending={trial.isPending}
        trialId={trialId}
        onStart={() => void startTrial()}
        spec={draft.spec}
      >
        {trialId && renderChat(trialId)}
      </TrialPane>
    </div>
  )
}

function isReadonly(definition?: Definition<AgentSpec>) {
  return definition?.readonly || definition?.status === "archived"
}

export function TemplateEditor({
  definition,
  models,
  onRun,
}: {
  definition?: Definition<TeamSpec>
  models: ModelInfo[]
  onRun: (id: string) => void
}) {
  const { t } = useTranslation("teams")
  const draft = useDefinitionDraft(
    "team",
    definition?.version.spec ?? emptyTeam(),
    definition,
    validTeamDraft,
  )
  const [error, setError] = useState<Error | null>(null)
  const preview = useTeamPreview()
  const readonly = definition?.readonly || definition?.status === "archived"
  useSavedDefinitionUrl("team", draft.record?.id, draft.saved)
  const valid = validTeamDraft(draft.spec)
  const publish = async () => {
    try {
      await preview.mutateAsync(draft.spec)
      await draft.publish()
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    }
  }
  return (
    <div className="scr min-h-0 flex-1 overflow-auto px-6 pb-8">
      <div className="mx-auto max-w-205 space-y-4">
        <WorkspaceActions>
          <SaveState
            published={draft.published}
            saved={draft.saved}
            busy={draft.pending}
            version={draft.record?.current_version_number}
          />
          {!readonly && (
            <button
              type="button"
              className={PRIMARY}
              disabled={!valid || draft.pending || draft.published}
              onClick={() => void publish()}
            >
              {t(draft.published ? "published" : "publishEnable")}
            </button>
          )}
        </WorkspaceActions>
        <Link
          to={`${paths.agents}?tab=templates`}
          className="text-n600 inline-flex items-center gap-1 text-xs"
        >
          <ArrowLeft className="size-3.5" />
          {t("backLibrary")}
        </Link>
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-medium">{draft.record?.name ?? t("newTemplate")}</h1>
          {draft.record?.status === "active" && (
            <button type="button" onClick={() => onRun(draft.record!.id)} className={BUTTON}>
              {t("run")}
            </button>
          )}
        </div>
        {(error ?? draft.error ?? preview.error) && (
          <p role="alert" className="text-danger text-sm">
            {(error ?? draft.error ?? preview.error)?.message}
          </p>
        )}
        <fieldset disabled={!!readonly}>
          <TeamForm spec={draft.spec} onChange={draft.setSpec} models={models} />
        </fieldset>
        <button
          type="button"
          className={BUTTON}
          disabled={!valid || preview.isPending}
          onClick={() => preview.mutate(draft.spec)}
        >
          {t("checkConfiguration")}
        </button>
        {preview.data && (
          <div className="bg-hairsoft space-y-2 rounded-xl p-4 text-xs">
            <p className="font-medium">{t("configurationReady")}</p>
            <p>
              {t("membersValue", { count: preview.data.members.length + 1 })}
            </p>
            {preview.data.members.map((member) => (
              <div key={member.alias}>
                <p>
                  {member.alias} · {member.model}
                </p>
                <CapabilityIssues summary={member} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

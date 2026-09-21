import { useState } from "react"
import { useLocation, useParams, useSearchParams } from "react-router"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"
import { useConfigQuery } from "@/features/chat/api/config"
import { ChatSurface } from "@/features/chat"
import { useDeleteSession, useProjectsQuery } from "@/features/workspace"
import {
  AgentEditor,
  TemplateEditor,
  DefinitionList,
  RunHistory,
  GenerateAgentDialog,
  StartChatDialog,
  useDefinition,
  type AgentSpec,
  type TeamSpec,
} from "@/features/agent-team"

const TABS = ["agents", "templates", "runs"]

function AgentPage({ id }: { id: string }) {
  const query = useDefinition<AgentSpec>("agent", id === "new" ? null : id)
  const config = useConfigQuery()
  const location = useLocation()
  const initialSpec = (location.state as { draftSpec?: AgentSpec } | null)?.draftSpec
  if (query.error) return <p className="text-danger p-6 text-sm">{query.error.message}</p>
  if (id !== "new" && !query.data) return <Spinner className="m-6 size-5" />
  return (
    <AgentEditor
      key={(location.state as { editorKey?: string } | null)?.editorKey ?? id}
      definition={query.data}
      initialSpec={initialSpec}
      models={config.data?.models ?? []}
      renderChat={(sessionId) => <ChatSurface key={sessionId} sessionId={sessionId} />}
    />
  )
}
function TemplatePage({ id, onRun }: { id: string; onRun: (id: string) => void }) {
  const location = useLocation()
  const query = useDefinition<TeamSpec>("team", id === "new" ? null : id)
  const config = useConfigQuery()
  if (query.error) return <p className="text-danger p-6 text-sm">{query.error.message}</p>
  if (id !== "new" && !query.data) return <Spinner className="m-6 size-5" />
  return (
    <TemplateEditor
      key={(location.state as { editorKey?: string } | null)?.editorKey ?? id}
      definition={query.data}
      models={config.data?.models ?? []}
      onRun={onRun}
    />
  )
}

export default function AgentLibraryRoute() {
  const { t } = useTranslation("teams")
  const { kind, definitionId } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = params.get("tab") ?? "agents"
  const [dialog, setDialog] = useState<"generate" | "chat" | "run" | null>(null)
  const [template, setTemplate] = useState<string | null>(null)
  const config = useConfigQuery()
  const projects = useProjectsQuery()
  const deleteSession = useDeleteSession()
  const onRun = (id: string) => {
    setTemplate(id)
    setDialog("run")
  }
  if (config.isLoading) return <Spinner className="m-6 size-5" />
  if (!config.data?.team_ui_enabled) return <p className="text-n600 p-6 text-sm">{t("featureUnavailable")}</p>
  return (
    <>
      {definitionId && kind === "agent" ? (
        <AgentPage id={definitionId} />
      ) : definitionId && kind === "team" ? (
        <TemplatePage id={definitionId} onRun={onRun} />
      ) : (
        <div className="scr min-h-0 flex-1 overflow-auto px-6.5 pt-1.5 pb-7">
          <div className="mx-auto w-full max-w-205 space-y-4.5">
            <div>
              <h1 className="text-2xl font-medium tracking-tight">{t("libraryTitle")}</h1>
              <p className="text-n600 mt-1 text-sm">{t("libraryHint")}</p>
            </div>
            <div className="flex gap-1" role="tablist" aria-label={t("libraryTitle")}>
              {TABS.map((name) => (
                <button
                  key={name}
                  role="tab"
                  type="button"
                  aria-selected={tab === name}
                  onClick={() => setParams({ tab: name })}
                  className={cn(
                    "rounded-full px-4 py-2 text-sm",
                    tab === name ? "bg-ink text-bg" : "text-n600 hover:bg-hairsoft",
                  )}
                >
                  {t(`libraryTabs.${name}`)}
                </button>
              ))}
            </div>
            {tab === "runs" ? (
              <RunHistory projects={projects.data ?? []} onDelete={deleteSession.mutateAsync} />
            ) : (
              <DefinitionList
                key={tab}
                kind={tab === "templates" ? "team" : "agent"}
                onRun={onRun}
                onChatCreate={() => {
                  setTemplate(null)
                  setDialog("chat")
                }}
                onGenerate={() => setDialog("generate")}
              />
            )}
          </div>
        </div>
      )}
      {dialog === "generate" && <GenerateAgentDialog onClose={() => setDialog(null)} />}
      {(dialog === "chat" || dialog === "run") && (
        <StartChatDialog
          templateId={template}
          projects={projects.data ?? []}
          onClose={() => setDialog(null)}
        />
      )}
    </>
  )
}

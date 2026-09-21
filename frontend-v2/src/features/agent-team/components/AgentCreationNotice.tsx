import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import { useDefinition, useDefinitionWrite } from "../api/teams"
import type { AgentSpec } from "../types"

export interface AutomaticAgentCreation {
  definition_id: string
  version_id: string
  revision: number
  name: string
}

export function AgentCreationNotice({ creation }: { creation: AutomaticAgentCreation }) {
  const { t } = useTranslation("teams")
  const current = useDefinition<AgentSpec>("agent", creation.definition_id)
  const undo = useDefinitionWrite<AgentSpec>("agent")
  const undone = current.data?.status === "draft" && current.data?.provenance?.autoapproval_undone === true
  const unchanged =
    current.data?.revision === creation.revision && current.data?.current_version_id === creation.version_id
  return (
    <div role="status" className="border-hair bg-card rounded-xl border p-3 text-sm">
      <p>{t(undone ? "agentCreationUndone" : "agentCreatedAutomatically", { name: creation.name })}</p>
      <div className="text-a700 mt-2 flex flex-wrap gap-3 text-xs">
        {unchanged && !undone && (
          <button
            type="button"
            disabled={undo.isPending}
            onClick={() =>
              undo.mutate({
                id: creation.definition_id,
                action: "undo-autoapproval",
                expected_revision: creation.revision,
                version_id: creation.version_id,
                key: `undo-auto:${creation.version_id}`,
              })
            }
          >
            {t("undoAgentCreation")}
          </button>
        )}
        <Link to={paths.agentEditor(creation.definition_id)}>{t("editInLibrary")}</Link>
      </div>
      {(current.error || undo.error) && (
        <p role="alert" className="text-danger mt-2 text-xs">
          {current.error?.message || undo.error?.message}
        </p>
      )}
    </div>
  )
}

// The resource centre page: project rail → list → preview. A view over the
// OSS asset ledger, not over a sandbox directory — the files here outlive any
// one conversation's container.
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { useResourceCenter } from "../hooks/useResourceCenter"
import { ResourceDetail } from "./ResourceDetail"
import { ResourceList } from "./ResourceList"
import { ScopeRail } from "./ScopeRail"

interface Props {
  /** Scope to open on, normally the project the last conversation ran in. */
  defaultProject: string
}

export function ResourceCenter({ defaultProject }: Props) {
  const { t } = useTranslation("resources")
  const centre = useResourceCenter(defaultProject)
  const pending = centre.pendingDelete ?? []

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <ScopeRail
        projects={centre.projects}
        project={centre.filters.project}
        source={centre.filters.source}
        onPickProject={centre.actions.setProject}
        onPickSource={centre.actions.setSource}
      />
      <ResourceList
        list={centre.list}
        filters={centre.filters}
        actions={centre.actions}
        selection={centre.selection}
        upload={centre.upload}
        mutations={{ ...centre.mutations, loadMore: centre.loadMore }}
      />
      <ResourceDetail resource={centre.selected} onDelete={centre.mutations.remove} />

      <Dialog open={pending.length > 0} onClose={centre.cancelDelete}>
        <DialogTitle>{t("delete.title")}</DialogTitle>
        <DialogBody>
          {pending.length === 1
            ? t("delete.one", { name: pending[0].name })
            : t("delete.many", { count: pending.length })}
        </DialogBody>
        <DialogActions>
          <button type="button" className="text-md text-n700" onClick={centre.cancelDelete}>
            {t("common:action.cancel", { ns: "common" })}
          </button>
          <button
            type="button"
            className="bg-danger text-md text-bg rounded-full px-4.5 py-2 font-medium"
            onClick={() => void centre.confirmDelete()}
          >
            {t("actions.delete")}
          </button>
        </DialogActions>
      </Dialog>
    </div>
  )
}

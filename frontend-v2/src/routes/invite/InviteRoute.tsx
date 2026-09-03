import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Link, useParams } from "react-router"
import { acceptInvitation, fetchWorkspaces } from "@/shared/api/workspaces"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { paths } from "@/shared/router/paths"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"

export default function InviteRoute() {
  const { t } = useTranslation("settings")
  const { token = "" } = useParams()
  const errorMessage = useApiErrorMessage()
  const [status, setStatus] = useState<"idle" | "loading" | "done">("idle")
  const [error, setError] = useState("")

  const accept = async () => {
    setStatus("loading")
    setError("")
    try {
      const result = await acceptInvitation(token)
      useWorkspaceStore.getState().setCurrent(result.workspace_id)
      const workspaces = await fetchWorkspaces()
      useWorkspaceStore
        .getState()
        .setItems(workspaces.items, workspaces.default_workspace_id)
      setStatus("done")
    } catch (cause) {
      setError(errorMessage(cause))
      setStatus("idle")
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg p-6 text-ink">
      <div className="flex w-full max-w-md flex-col gap-4 rounded-3xl border border-hair bg-panel p-7 text-center">
        <h1 className="text-xl font-medium">{t("team.acceptTitle")}</h1>
        <p className="text-sm text-n600">
          {status === "done" ? t("team.accepted") : t("team.acceptHint")}
        </p>
        {error && <p className="text-sm text-red-600">{error}</p>}
        {status === "done" ? (
          <Link to={paths.app} className="rounded-full bg-ink px-5 py-2.5 text-sm text-bg">
            {t("team.enterWorkspace")}
          </Link>
        ) : (
          <button
            type="button"
            disabled={!token || status === "loading"}
            onClick={() => void accept()}
            className="rounded-full bg-ink px-5 py-2.5 text-sm text-bg disabled:opacity-50"
          >
            {t("team.accept")}
          </button>
        )}
      </div>
    </main>
  )
}

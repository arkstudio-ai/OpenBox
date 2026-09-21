import { useEffect } from "react"
import { useNavigate, useParams } from "react-router"
import { paths } from "@/shared/router/paths"

/** Keep a newly autosaved draft addressable after reload, without remounting
 * the editor or throwing away its active trial and in-flight form edits. */
export function useSavedDefinitionUrl(kind: "agent" | "team", id: string | undefined, saved: boolean) {
  const params = useParams()
  const navigate = useNavigate()
  useEffect(() => {
    if (!id || !saved || params.definitionId !== "new") return
    void navigate(kind === "agent" ? paths.agentEditor(id) : paths.teamEditor(id), {
      replace: true,
      state: { editorKey: "new" },
    })
  }, [id, saved, params.definitionId, kind, navigate])
}

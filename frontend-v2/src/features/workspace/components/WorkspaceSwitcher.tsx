import { ChevronsUpDown } from "lucide-react"
import { useNavigate } from "react-router"
import { paths } from "@/shared/router/paths"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useTranslation } from "react-i18next"

export function WorkspaceSwitcher() {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  const items = useWorkspaceStore((state) => state.items)
  const currentId = useWorkspaceStore((state) => state.currentId)
  const setCurrent = useWorkspaceStore((state) => state.setCurrent)

  if (items.length <= 1) return null
  return (
    <label className="relative mb-2 flex h-9 items-center rounded-full border border-hair bg-bg px-3 text-sm text-ink">
      <select
        value={currentId ?? ""}
        onChange={(event) => {
          setCurrent(event.target.value)
          navigate(paths.app)
        }}
        className="min-w-0 flex-1 appearance-none bg-transparent pe-6 outline-none"
        aria-label={t("workspaceSwitcher")}
      >
        {items.map((workspace) => (
          <option key={workspace.id} value={workspace.id}>
            {workspace.name}
          </option>
        ))}
      </select>
      <ChevronsUpDown className="pointer-events-none absolute end-3 text-n600" size={14} />
    </label>
  )
}

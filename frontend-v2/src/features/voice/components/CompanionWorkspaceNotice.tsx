import { useTranslation } from "react-i18next"

interface Props {
  workspaceName?: string
  onReturn?: () => void
}
/** Never show another workspace's conversation as if it belonged to the ongoing call. */
export function CompanionWorkspaceNotice({ workspaceName, onReturn }: Props) {
  const { t } = useTranslation("companion")
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
      <p className="text-n700 text-sm leading-6">
        {workspaceName ? t("call.otherWorkspace", { name: workspaceName }) : t("call.workspaceUnavailable")}
      </p>
      {onReturn && (
        <button
          type="button"
          onClick={onReturn}
          className="border-hair hover:bg-hairsoft min-h-11 rounded-full border px-4 text-sm"
        >
          {t("call.returnWorkspace")}
        </button>
      )}
    </div>
  )
}

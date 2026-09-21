import { useTranslation } from "react-i18next"

/** Confirmation content comes from the validated server proposal. */
export function McpAuthorizationDetail({ refs }: { refs: unknown }) {
  const { t } = useTranslation("teams")
  if (!Array.isArray(refs) || !refs.length) return null
  return (
    <div className="bg-a100 text-a800 mt-2 space-y-1 rounded-lg p-2 text-xs">
      <strong>{t("mcpServices")}</strong>
      {refs.map((ref: unknown, index) => {
        if (!ref || typeof ref !== "object" || !("server" in ref) || typeof ref.server !== "string")
          return null
        const tools =
          "tools" in ref && Array.isArray(ref.tools)
            ? ref.tools.filter((tool): tool is string => typeof tool === "string")
            : ["*"]
        return (
          <p key={`${ref.server}:${index}`} className="break-words">
            {ref.server}: {tools.join(", ") || t("mcpNoTools")}
          </p>
        )
      })}
    </div>
  )
}

import { useTranslation } from "react-i18next"
import type { TeamPolicy } from "../types"
import { BUTTON, Field, INPUT } from "./FormFields"

const EDIT_TOOLS = ["write", "edit", "multiedit", "apply_patch"]

export function PermissionScopes({
  policy,
  onChange,
}: {
  policy: TeamPolicy
  onChange: (rules: NonNullable<TeamPolicy["permission_rules"]>) => void
}) {
  const { t } = useTranslation("teams")
  const rules = policy.permission_rules ?? []
  const choices = [
    ...new Set(policy.delegable_tools.map((tool) => (EDIT_TOOLS.includes(tool) ? "edit" : tool))),
  ]
  return (
    <div className="space-y-2">
      <p className="text-sm">{t("operationScopes")}</p>
      <p className="text-n600 text-xs">{t("operationScopesHint")}</p>
      {rules.map((rule, index) => (
        <div key={index} className="border-hair space-y-2 rounded-lg border p-3">
          <label className="text-n600 block text-xs">
            {t("scopeOperation")}
            <select
              className={`${INPUT} mt-1.5`}
              value={rule.permission}
              onChange={(event) =>
                onChange(
                  rules.map((item, position) =>
                    position === index ? { ...item, permission: event.target.value } : item,
                  ),
                )
              }
            >
              {!choices.includes(rule.permission) && (
                <option value={rule.permission}>{rule.permission}</option>
              )}
              {choices.map((choice) => (
                <option key={choice} value={choice}>
                  {choice}
                </option>
              ))}
            </select>
          </label>
          <Field
            label={t("scopePattern")}
            value={rule.pattern}
            onChange={(pattern) =>
              onChange(rules.map((item, position) => (position === index ? { ...item, pattern } : item)))
            }
          />
          <button
            type="button"
            className={BUTTON}
            onClick={() => onChange(rules.filter((_, position) => position !== index))}
          >
            {t("removeScope")}
          </button>
        </div>
      ))}
      <button
        type="button"
        className={BUTTON}
        disabled={!choices.length || rules.length >= 128}
        onClick={() =>
          onChange([
            ...rules,
            { permission: choices.includes("edit") ? "edit" : choices[0], pattern: "", action: "allow" },
          ])
        }
      >
        {t("addScope")}
      </button>
    </div>
  )
}

import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { Bot, ArrowUpRight } from "lucide-react"
import type { QuestionItem } from "@/shared/types/api"
import { paths } from "@/shared/router/paths"
import { McpAuthorizationDetail } from "./McpAuthorizationDetail"

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}
function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}

export function AgentProposalDetail({ item }: { item: QuestionItem }) {
  const { t } = useTranslation("teams")
  const detail = record(item.detail)
  if (detail.kind !== "agent_proposal") return null
  const spec = record(detail.spec)
  const previous = record(detail.previous_spec)
  const changed = Object.keys(previous).filter(
    (key) => JSON.stringify(previous[key]) !== JSON.stringify(spec[key]),
  )
  const capability = record(detail.capability_summary)
  const skills = Array.isArray(capability.skills) ? capability.skills.map(record) : []
  const tools = Array.isArray(spec.tool_allowlist) ? spec.tool_allowlist.map(text) : []
  const tiers = record(capability.tool_tiers)
  const warnings = Array.isArray(capability.warnings) ? capability.warnings.map(record) : []
  return (
    <div className="border-hair bg-bg space-y-3 rounded-xl border p-4">
      <div className="flex items-center gap-2">
        <Bot className="text-a700 size-5" />
        <strong className="text-sm">{text(spec.name)}</strong>
      </div>
      <p className="text-n600 text-sm">{text(spec.description)}</p>
      <p className="text-n600 text-xs">{text(spec.when_to_use)}</p>
      {changed.length > 0 && (
        <details className="border-hair border-t pt-2">
          <summary className="cursor-pointer text-xs">
            {t("proposedChanges", { count: changed.length })}
          </summary>
          <dl className="text-n600 mt-2 space-y-2 text-xs">
            {changed.map((key) => (
              <div key={key}>
                <dt className="font-medium">{t(`specFields.${key}`, { defaultValue: key })}</dt>
                <dd className="mt-1 [overflow-wrap:anywhere] whitespace-pre-wrap">
                  {JSON.stringify(previous[key], null, 2)} → {JSON.stringify(spec[key], null, 2)}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      <p className="text-n600 text-xs">
        {t("model")}: {text(capability.model)}
      </p>
      <div className="space-y-1 text-xs">
        <strong>{t("skills")}</strong>
        {skills.length === 0 && <p className="text-n600">{t("noSelectedSkills")}</p>}
        {skills.map((skill) => (
          <p key={text(skill.name)}>
            <span>{text(skill.name)}</span>
            <span className="text-n600 ms-2">{text(skill.description)}</span>
          </p>
        ))}
      </div>
      <div className="space-y-2 text-xs">
        <strong>{t("tools")}</strong>
        <div className="flex flex-wrap gap-1.5">
          {tools.map((tool) => (
            <span
              key={tool}
              className={
                tiers[tool] === "T0"
                  ? "bg-hairsoft rounded-full px-2 py-1"
                  : "bg-a100 text-a800 rounded-full px-2 py-1"
              }
            >
              {tool}
            </span>
          ))}
        </div>
      </div>
      <McpAuthorizationDetail refs={spec.mcp_refs} />
      {warnings.map((warning) => (
        <p key={text(warning.skill)} className="text-a700 text-xs">
          {t("missingSkillTools", {
            skill: text(warning.skill),
            tools: Array.isArray(warning.tools) ? warning.tools.map(text).join(", ") : "",
          })}
        </p>
      ))}
      <details className="border-hair border-t pt-2">
        <summary className="cursor-pointer text-xs">{t("instruction")}</summary>
        <p className="text-n600 mt-2 text-xs whitespace-pre-wrap">{text(spec.instruction)}</p>
      </details>
      <Link
        to={paths.agentEditor(text(detail.definition_id))}
        className="text-a700 inline-flex items-center gap-1 text-xs"
      >
        {t("editInLibrary")}
        <ArrowUpRight className="size-3" />
      </Link>
    </div>
  )
}

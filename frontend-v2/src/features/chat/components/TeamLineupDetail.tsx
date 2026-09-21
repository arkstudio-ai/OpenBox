import { useTranslation } from "react-i18next"
import { Bot, Users, ShieldCheck } from "lucide-react"
import type { QuestionItem } from "@/shared/types/api"
import { McpAuthorizationDetail } from "./McpAuthorizationDetail"

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}
function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : ""
}

/** The question remains the single confirmation control, including on mobile.
 * Only the explanatory body is richer for clients that know this kind. */
export function TeamLineupDetail({ item }: { item: QuestionItem }) {
  const { t } = useTranslation("teams")
  const detail = record(item.detail)
  if (detail.kind !== "team_lineup") return null
  const spec = record(detail.spec)
  const policy = record(spec.policy)
  const coordinator = record(detail.coordinator)
  const members = Array.isArray(detail.members) ? detail.members.map(record) : []
  const paid = record(record(detail.grant).paid_tools)
  const rules = record(detail.grant).permission_rules
  const scopes = Array.isArray(rules) ? rules.map(record) : []
  return (
    <div className="border-hair bg-bg rounded-xl border p-3">
      <div className="flex items-center gap-2 py-2 text-sm">
        <Users className="text-a700 size-4" />
        <strong>{text(spec.name)}</strong>
      </div>
      <div className="border-hair flex items-center gap-3 border-b py-3">
        <Bot className="text-a700 size-5" />
        <span className="flex-1 text-sm">{t("coordinator")}</span>
        <span className="text-n600 text-xs">{text(coordinator.model)}</span>
      </div>
      {members.map((member) => (
        <div key={text(member.alias)} className="border-hair flex gap-3 border-b py-3">
          <Bot className="text-n600 mt-0.5 size-5 flex-none" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <strong>{text(member.name)}</strong>
              <span className="text-n600 text-xs">{text(member.model)}</span>
              <span className="bg-hairsoft rounded-full px-2 py-0.5 text-xs">
                {t(`source.${text(member.source)}`)}
              </span>
            </div>
            <p className="text-n600 mt-1 text-xs">
              {text(member.responsibility) || text(member.description)}
            </p>
            {Array.isArray(member.skills) && member.skills.length > 0 && (
              <p className="text-n600 mt-1 text-xs">
                {t("skills")}: {member.skills.map((skill) => text(record(skill).name)).join(" · ")}
              </p>
            )}
            {Array.isArray(member.tool_ids) && (
              <p className="text-n600 mt-1 text-xs break-words">
                {t("tools")}: {member.tool_ids.map(text).join(", ")}
              </p>
            )}
            <McpAuthorizationDetail refs={member.mcp_refs} />
          </div>
        </div>
      ))}
      <div className="text-n600 flex flex-wrap gap-x-4 gap-y-2 pt-3 text-xs">
        <span>{t("concurrencyValue", { count: Number(policy.max_concurrent_members ?? 0) })}</span>
        <span>
          {t("durationValue", { count: Math.ceil(Number(policy.max_wall_time_seconds ?? 0) / 60) })}
        </span>
      </div>
      <p className="text-n600 mt-2 flex gap-1.5 text-xs">
        <ShieldCheck className="size-3.5 flex-none" />
        {t(policy.member_selection === "explicit_only" ? "fixedTeam" : "supplementEnabled")}
      </p>
      <McpAuthorizationDetail refs={record(detail.grant).mcp_refs} />
      {scopes.length > 0 && (
        <div className="bg-a100 text-a800 mt-3 rounded-lg p-2 text-xs">
          <p>{t("operationScopes")}</p>
          <p className="mt-1">{t("operationScopesHint")}</p>
          {scopes.map((rule, index) => (
            <p key={index} className="mt-1 break-all">
              {text(rule.permission)}: {text(rule.pattern)}
            </p>
          ))}
        </div>
      )}
      {Object.keys(paid).length > 0 && (
        <div className="bg-a100 text-a800 mt-3 rounded-lg p-2 text-xs">
          {t("paidAuthorization")}
          {Object.keys(paid).map((name) => (
            <p key={name}>
              {name}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

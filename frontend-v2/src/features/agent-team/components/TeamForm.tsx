import { McpFields } from "./McpFields"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { PermissionScopes } from "./PermissionScopes"
import { Check, Plus, X } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { ModelInfo } from "@/shared/types/api"
import { useCatalogSkills, useDefinitions, useVersions } from "../api/teams"
import type { AgentSpec, MemberSpec, TeamSpec } from "../types"
import { BUTTON, Field, FormSection, INPUT, JsonField } from "./FormFields"
import { AgentForm } from "./AgentForm"

function MemberVersions({
  member,
  onChange,
}: {
  member: MemberSpec
  onChange: (member: MemberSpec) => void
}) {
  const { t } = useTranslation("teams")
  const versions = useVersions<AgentSpec>(
    "agent",
    member.agent_ref ?? "",
    Boolean(member.agent_ref && !member.agent_ref.startsWith("builtin:")),
  )
  return (
    <label className="text-n600 block text-xs">
      {t("versionPolicy")}
      <select
        value={member.version_policy === "pinned" ? (member.version_id ?? "") : "latest"}
        className={`${INPUT} mt-1.5`}
        onChange={(event) =>
          onChange(
            event.target.value === "latest"
              ? { ...member, version_policy: "latest_at_run_start" }
              : { ...member, version_id: event.target.value, version_policy: "pinned" },
          )
        }
      >
        <option value="latest">{t("latestVersion")}</option>
        {member.version_id && <option value={member.version_id}>{t("pinnedVersion")}</option>}
        {versions.data?.pages
          .flatMap((page) => page.items)
          .filter((item) => item.published && item.id !== member.version_id)
          .map((item) => (
            <option key={item.id} value={item.id}>
              {t("version", { version: item.version })}
            </option>
          ))}
      </select>
      {member.version_policy === "latest_at_run_start" && !member.agent_ref?.startsWith("builtin:") && (
        <button
          type="button"
          className="mt-2 underline"
          onClick={() => onChange({ ...member, version_policy: "pinned" })}
        >
          {t("chooseVersion")}
        </button>
      )}
    </label>
  )
}

function MemberFields({
  member,
  models,
  onChange,
  onRemove,
}: {
  member: MemberSpec
  models: ModelInfo[]
  onChange: (member: MemberSpec) => void
  onRemove: () => void
}) {
  const { t } = useTranslation("teams")
  const skills = useCatalogSkills()
  return (
    <div className={cn("border-hair space-y-3 rounded-lg border p-3", !member.enabled && "opacity-60")}>
      <div className="flex items-center gap-2">
        <Field
          label={t("alias")}
          value={member.alias}
          onChange={(alias) => onChange({ ...member, alias })}
          maxLength={40}
        />
        <button
          type="button"
          onClick={onRemove}
          className="hover:bg-hairsoft ms-auto rounded-full p-2"
          aria-label={t("removeMember")}
        >
          <X className="size-4" />
        </button>
      </div>
      <Field
        label={t("responsibility")}
        multiline
        value={member.responsibility}
        onChange={(responsibility) => onChange({ ...member, responsibility })}
        maxLength={2000}
      />
      <label className="text-n600 block text-xs">
        {t("modelOverride")}
        <select
          value={member.model_override ?? ""}
          className={`${INPUT} mt-1.5`}
          onChange={(event) => onChange({ ...member, model_override: event.target.value || null })}
        >
          <option value="">{t("definitionModel")}</option>
          {models.map((model) => (
            <option key={model.id} value={model.id}>
              {model.name}
            </option>
          ))}
        </select>
      </label>
      <details>
        <summary className="text-n600 cursor-pointer text-xs">
          {t("additionalSkills", { count: member.additional_skills.length })}
        </summary>
        <div className="mt-2 space-y-1">
          {skills.data?.map((skill) => (
            <label key={skill.name} className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={member.additional_skills.some((ref) => ref.name === skill.name)}
                onChange={(event) =>
                  onChange({
                    ...member,
                    additional_skills: event.target.checked
                      ? [...member.additional_skills, { name: skill.name }]
                      : member.additional_skills.filter((ref) => ref.name !== skill.name),
                  })
                }
              />
              {skill.name}
            </label>
          ))}
        </div>
      </details>
      {member.agent_ref && <MemberVersions member={member} onChange={onChange} />}
      {member.inline && (
        <details>
          <summary className="text-n600 cursor-pointer text-xs">
            {t("inlineConfiguration", { name: member.inline.name })}
          </summary>
          <p className="text-n600 my-3 text-xs">{t("inlineConfigurationHint")}</p>
          <AgentForm
            spec={member.inline}
            models={models}
            onChange={(inline) => onChange({ ...member, inline })}
          />
        </details>
      )}
      <label className="flex items-center gap-2 text-xs">
        <input
          type="checkbox"
          checked={member.enabled}
          onChange={(event) => onChange({ ...member, enabled: event.target.checked })}
        />
        {t("memberEnabled")}
      </label>
    </div>
  )
}

function Members({
  spec,
  onChange,
  models,
}: {
  spec: TeamSpec
  onChange: (spec: TeamSpec) => void
  models: ModelInfo[]
}) {
  const { t } = useTranslation("teams")
  const agents = useDefinitions<AgentSpec>("agent", "", "active")
  const [add, setAdd] = useState("")
  const rows = [
    ...(agents.data?.pages.flatMap((page) => page.items) ?? []),
    ...(agents.data?.pages[0]?.builtin ?? []),
  ].filter((definition) => definition.id !== "builtin:team-coordinator")
  const supplement = spec.policy.member_selection === "coordinator_select"
  const addMember = () => {
    const definition = rows.find((row) => row.id === add)
    if (!definition) return
    const baseAlias = definition.name.replace(/[^\p{L}\p{N}_-]/gu, "-").slice(0, 36)
    let alias = baseAlias
    let number = 2
    while (spec.preset_members.some((member) => member.alias === alias)) alias = `${baseAlias}-${number++}`
    onChange({
      ...spec,
      preset_members: [
        ...spec.preset_members,
        {
          alias,
          agent_ref: definition.id,
          version_id: definition.current_version_id,
          version_policy: "latest_at_run_start",
          responsibility: "",
          additional_skills: [],
          enabled: true,
        },
      ],
    })
    setAdd("")
  }
  return (
    <FormSection title={t("tabs.members")}>
      <label className="text-n600 block text-xs">
        {t("coordinatorModel")}
        <select
          value={spec.coordinator.model ?? ""}
          onChange={(event) =>
            onChange({ ...spec, coordinator: { ...spec.coordinator, model: event.target.value || null } })
          }
          className={`${INPUT} mt-1.5`}
        >
          <option value="">{t("followDefault")}</option>
          {models.map((model) => (
            <option key={model.id} value={model.id}>
              {model.name}
            </option>
          ))}
        </select>
      </label>
      {spec.preset_members.map((member, index) => (
        <MemberFields
          key={index}
          member={member}
          models={models}
          onChange={(value) =>
            onChange({
              ...spec,
              preset_members: spec.preset_members.map((item, at) => (at === index ? value : item)),
            })
          }
          onRemove={() =>
            onChange({ ...spec, preset_members: spec.preset_members.filter((_, at) => at !== index) })
          }
        />
      ))}
      <div className="flex gap-2">
        <select
          aria-label={t("addMember")}
          value={add}
          onChange={(event) => setAdd(event.target.value)}
          className={INPUT}
        >
          <option value="">{t("chooseAgent")}</option>
          {rows.map((definition) => (
            <option key={definition.id} value={definition.id}>
              {definition.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!add || spec.preset_members.length >= spec.policy.max_members - 1}
          onClick={addMember}
          className={`${BUTTON} flex items-center gap-1 whitespace-nowrap`}
        >
          <Plus className="size-4" />
          {t("addMember")}
        </button>
      </div>
      {agents.hasNextPage && (
        <button type="button" className={BUTTON} onClick={() => void agents.fetchNextPage()}>
          {t("loadMore")}
        </button>
      )}
      <button
        type="button"
        aria-pressed={supplement}
        onClick={() =>
          onChange({
            ...spec,
            policy: { ...spec.policy, member_selection: supplement ? "explicit_only" : "coordinator_select" },
          })
        }
        className="flex items-center gap-2 text-start text-sm"
      >
        <span
          className={cn(
            "border-hair flex size-4.5 flex-none items-center justify-center rounded-full border",
            supplement && "bg-s600 text-bg",
          )}
        >
          <Check className={cn("size-3", !supplement && "opacity-0")} />
        </span>
        {t("allowSupplement")}
      </button>
      <p className="text-n600 text-xs">{t("supplementHint")}</p>
      {!supplement && !spec.preset_members.some((member) => member.enabled) && (
        <p className="text-danger text-xs">{t("emptyRosterError")}</p>
      )}
    </FormSection>
  )
}

function Limits({
  spec,
  onChange,
  models,
}: {
  spec: TeamSpec
  onChange: (spec: TeamSpec) => void
  models: ModelInfo[]
}) {
  const { t } = useTranslation("teams")
  const catalogue = useDefinitions<AgentSpec>("agent")
  const skills = useCatalogSkills()
  const policy = spec.policy
  const update = (value: Partial<TeamSpec["policy"]>) =>
    onChange({ ...spec, policy: { ...policy, ...value } })
  const tools = [...new Set(Object.values(catalogue.data?.pages[0]?.tool_presets ?? {}).flat())]
  return (
    <FormSection title={t("scopeLimits")}>
      <details open>
        <summary className="cursor-pointer text-sm">{t("executionLimits")}</summary>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <Field
            label={t("maxSeconds")}
            type="number"
            min={30}
            max={86400}
            value={policy.max_wall_time_seconds}
            onChange={(value) => update({ max_wall_time_seconds: Number(value) })}
          />
          <Field
            label={t("maxMembers")}
            type="number"
            min={2}
            max={8}
            value={policy.max_members}
            onChange={(value) => update({ max_members: Number(value) })}
          />
          <Field
            label={t("maxConcurrent")}
            type="number"
            min={1}
            max={7}
            value={policy.max_concurrent_members}
            onChange={(value) => update({ max_concurrent_members: Number(value) })}
          />
        </div>
      </details>
      <details>
        <summary className="cursor-pointer text-sm">{t("memberCapabilities")}</summary>
        <div className="mt-3 space-y-4">
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={policy.member_creation === "run_scoped"}
              onChange={(event) =>
                update({ member_creation: event.target.checked ? "run_scoped" : "disabled" })
              }
            />
            {t("allowTemporary")}
          </label>
          <div>
            <p className="text-n600 mb-2 text-xs">{t("allowedModels")}</p>
            {models.map((model) => (
              <label key={model.id} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={policy.allowed_models.includes(model.id)}
                  onChange={(event) =>
                    update({
                      allowed_models: event.target.checked
                        ? [...policy.allowed_models, model.id]
                        : policy.allowed_models.filter((value) => value !== model.id),
                    })
                  }
                />
                {model.name}
              </label>
            ))}
          </div>
          <div>
            <p className="text-n600 mb-2 text-xs">{t("tools")}</p>
            <div className="flex flex-wrap gap-2">
              {tools.map((tool) => (
                <label key={tool} className="flex items-center gap-1 text-xs">
                  <input
                    type="checkbox"
                    checked={policy.delegable_tools.includes(tool)}
                    onChange={(event) =>
                      update({
                        delegable_tools: event.target.checked
                          ? [...policy.delegable_tools, tool]
                          : policy.delegable_tools.filter((value) => value !== tool),
                      })
                    }
                  />
                  {tool}
                </label>
              ))}
            </div>
          </div>
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={policy.allowed_skills === null}
              onChange={(event) => update({ allowed_skills: event.target.checked ? null : [] })}
            />
            {t("allAccessibleSkills")}
          </label>
          {policy.allowed_skills !== null && (
            <div className="space-y-1">
              {skills.data?.map((skill) => (
                <label key={skill.name} className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={policy.allowed_skills?.some((ref) => ref.name === skill.name) ?? false}
                    onChange={(event) =>
                      update({
                        allowed_skills: event.target.checked
                          ? [...(policy.allowed_skills ?? []), { name: skill.name }]
                          : policy.allowed_skills?.filter((ref) => ref.name !== skill.name),
                      })
                    }
                  />
                  {skill.name}
                </label>
              ))}
            </div>
          )}
          <McpFields value={policy.mcp_refs} onChange={(mcp_refs) => update({ mcp_refs })} />
          <PermissionScopes policy={policy} onChange={(permission_rules) => update({ permission_rules })} />
          <p className="text-n600 text-xs">{t("paidAuthorizationHint")}</p>
          <JsonField
            label={t("paidAuthorization")}
            value={Object.fromEntries(Object.keys(policy.paid_tools).map((tool) => [tool, { authorized: true }]))}
            onChange={(value) => update({ paid_tools: (value ?? {}) as TeamSpec["policy"]["paid_tools"] })}
          />
        </div>
      </details>
      <details>
        <summary className="cursor-pointer text-sm">{t("acceptance")} </summary>
        <div className="mt-3 space-y-4">
          <label className="text-n600 block text-xs">
            {t("acceptanceMode")}
            <select
              value={spec.acceptance_mode}
              className={`${INPUT} mt-1.5`}
              onChange={(event) =>
                onChange({ ...spec, acceptance_mode: event.target.value as TeamSpec["acceptance_mode"] })
              }
            >
              <option value="coordinator">{t("coordinatorReview")}</option>
              <option value="auto">{t("automaticReview")}</option>
            </select>
          </label>
          <JsonField
            label={t("inputSchema")}
            value={spec.goal_input_schema}
            onChange={(value) =>
              onChange({ ...spec, goal_input_schema: value as TeamSpec["goal_input_schema"] })
            }
          />
          <JsonField
            label={t("outputSchema")}
            value={spec.result_schema}
            onChange={(value) => onChange({ ...spec, result_schema: value as TeamSpec["result_schema"] })}
          />
          <Field
            label={t("resources")}
            multiline
            value={spec.resource_refs.join("\n")}
            onChange={(value) => onChange({ ...spec, resource_refs: value.split("\n").filter(Boolean) })}
          />
        </div>
      </details>
    </FormSection>
  )
}

export function TeamForm({
  spec,
  onChange,
  models,
}: {
  spec: TeamSpec
  onChange: (spec: TeamSpec) => void
  models: ModelInfo[]
}) {
  const { t } = useTranslation("teams")
  return (
    <div className="space-y-4">
      <FormSection title={t("basicInfo")}>
        <Field
          label={t("name")}
          value={spec.name}
          onChange={(name) => onChange({ ...spec, name })}
          required
          maxLength={80}
        />
        <Field
          label={t("description")}
          value={spec.description}
          onChange={(description) => onChange({ ...spec, description })}
          multiline
          maxLength={1000}
        />
      </FormSection>
      <Members spec={spec} onChange={onChange} models={models} />
      <Limits spec={spec} onChange={onChange} models={models} />
    </div>
  )
}

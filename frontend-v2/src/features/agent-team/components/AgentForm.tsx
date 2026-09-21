import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Check } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { ModelInfo } from "@/shared/types/api"
import { useCatalogSkills, useDefinitions, useModelCapabilities } from "../api/teams"
import type { AgentSpec } from "../types"
import { BUTTON, Field, FormSection, INPUT, JsonField } from "./FormFields"

import { McpFields } from "./McpFields"

const SKILL_MODES = ["selected", "all_accessible"] as const
const TOOL_CATEGORIES = ["T0", "T1", "T2", "MCP"] as const

function SkillsAndTools({ spec, onChange }: { spec: AgentSpec; onChange: (spec: AgentSpec) => void }) {
  const { t } = useTranslation("teams")
  const [search, setSearch] = useState("")
  const skills = useCatalogSkills()
  const catalogue = useDefinitions<AgentSpec>("agent")
  const presets = catalogue.data?.pages[0]?.tool_presets ?? {}
  const available = [
    ...new Set([...Object.values(presets).flat(), ...(catalogue.data?.pages[0]?.plugin_tools ?? [])]),
  ]
  const selected =
    skills.data?.filter((skill) => spec.skill_refs.some((ref) => ref.name === skill.name)) ?? []
  const missing = [...new Set(selected.flatMap((skill) => skill.allowed_tools ?? []))].filter(
    (tool) => !spec.tool_allowlist.includes(tool),
  )
  return (
    <>
      <FormSection title={t("skills")}>
        <div className="flex gap-1">
          {SKILL_MODES.map((mode) => (
            <button
              key={mode}
              type="button"
              aria-pressed={spec.skill_mode === mode}
              onClick={() => onChange({ ...spec, skill_mode: mode })}
              className={cn(
                "rounded-full px-3 py-1.5 text-xs",
                spec.skill_mode === mode ? "bg-ink text-bg" : "text-n600 hover:bg-hairsoft",
              )}
            >
              {t(`skillMode.${mode}`)}
            </button>
          ))}
        </div>
        {spec.skill_mode === "selected" && (
          <>
            <input
              aria-label={t("searchSkills")}
              placeholder={t("searchSkills")}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className={INPUT}
            />
            <div className="scr max-h-52 space-y-1 overflow-auto">
              {skills.data
                ?.filter((skill) =>
                  `${skill.name} ${skill.description}`
                    .toLocaleLowerCase()
                    .includes(search.toLocaleLowerCase()),
                )
                .map((skill) => {
                  const checked = spec.skill_refs.some((ref) => ref.name === skill.name)
                  return (
                    <button
                      key={skill.name}
                      type="button"
                      aria-pressed={checked}
                      onClick={() =>
                        onChange({
                          ...spec,
                          skill_refs: checked
                            ? spec.skill_refs.filter((ref) => ref.name !== skill.name)
                            : [...spec.skill_refs, { name: skill.name }],
                        })
                      }
                      className="hover:bg-hairsoft flex w-full items-start gap-2 rounded-lg p-2 text-start"
                    >
                      <span
                        className={cn(
                          "border-hair mt-0.5 flex size-4.5 flex-none items-center justify-center rounded-full border",
                          checked && "bg-s600 text-bg",
                        )}
                      >
                        <Check className={cn("size-3", !checked && "opacity-0")} />
                      </span>
                      <span>
                        <strong className="block text-xs">{skill.name}</strong>
                        <span className="text-n600 mt-0.5 block text-xs">{skill.description}</span>
                      </span>
                    </button>
                  )
                })}
            </div>
            {skills.error && <p className="text-danger text-xs">{skills.error.message}</p>}
          </>
        )}
        {missing.length > 0 && (
          <div className="bg-hairsoft space-y-2 rounded-lg p-3 text-xs">
            <p>{t("missingTools", { tools: missing.join(", ") })}</p>
            <button
              type="button"
              className={BUTTON}
              onClick={() =>
                onChange({
                  ...spec,
                  tool_allowlist: [
                    ...new Set([
                      ...spec.tool_allowlist,
                      ...missing.filter((tool) => available.includes(tool)),
                    ]),
                  ],
                })
              }
            >
              {t("addRequiredTools")}
            </button>
            {missing.some((tool) => !available.includes(tool)) && (
              <p className="text-a700">{t("rootOnlyTools")}</p>
            )}
          </div>
        )}
      </FormSection>
      <FormSection title={t("tools")}>
        <div className="grid grid-cols-2 gap-2">
          {Object.entries(presets).map(([name, tools]) => (
            <button
              key={name}
              type="button"
              onClick={() => onChange({ ...spec, tool_allowlist: tools })}
              className={cn(
                "rounded-lg border px-3 py-2 text-start text-xs",
                [...spec.tool_allowlist].sort().join() === [...tools].sort().join()
                  ? "border-ink bg-hairsoft"
                  : "border-hair",
              )}
            >
              {t(`preset.${name}`)}
            </button>
          ))}
        </div>
        <details>
          <summary className="text-n600 cursor-pointer text-xs">
            {t("customizeTools", { count: spec.tool_allowlist.length })}
          </summary>
          <div className="mt-3 flex flex-wrap gap-2">
            {available.map((tool) => (
              <label key={tool} className="flex items-center gap-1 text-xs">
                <input
                  type="checkbox"
                  checked={spec.tool_allowlist.includes(tool)}
                  onChange={(event) =>
                    onChange({
                      ...spec,
                      tool_allowlist: event.target.checked
                        ? [...spec.tool_allowlist, tool]
                        : spec.tool_allowlist.filter((value) => value !== tool),
                    })
                  }
                  className="accent-accent"
                />
                {tool}
              </label>
            ))}
          </div>
        </details>
      </FormSection>
      <McpFields value={spec.mcp_refs} onChange={(mcp_refs) => onChange({ ...spec, mcp_refs })} />
    </>
  )
}
function Advanced({
  spec,
  onChange,
  models,
}: {
  spec: AgentSpec
  onChange: (spec: AgentSpec) => void
  models: ModelInfo[]
}) {
  const { t } = useTranslation("teams")
  const capabilities = useModelCapabilities()
  const chosen = capabilities.data?.models.find((model) => model.id === spec.default_model)
  const variants = chosen?.reasoning_variants ?? []
  const unsupportedReasoning = !!spec.reasoning && !!capabilities.data && !variants.includes(spec.reasoning)
  return (
    <details className="border-hair bg-card rounded-xl border p-4">
      <summary className="cursor-pointer text-sm font-medium">{t("advanced")}</summary>
      <div className="mt-4 space-y-4">
        <label className="text-n600 block text-xs">
          {t("defaultModel")}
          <select
            value={spec.default_model ?? ""}
            onChange={(event) =>
              onChange({
                ...spec,
                default_model: event.target.value || null,
                model_locked: event.target.value ? spec.model_locked : false,
                reasoning: null,
              })
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
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={spec.model_locked}
            disabled={!spec.default_model}
            onChange={(event) => onChange({ ...spec, model_locked: event.target.checked })}
            className="accent-accent"
          />
          {t("lockModel")}
        </label>
        <div>
          <span className="text-n600 text-xs">{t("allowedModels")}</span>
          <div className="mt-2 space-y-1">
            {models.map((model) => (
              <label key={model.id} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={spec.allowed_models.includes(model.id)}
                  onChange={(event) =>
                    onChange({
                      ...spec,
                      allowed_models: event.target.checked
                        ? [...spec.allowed_models, model.id]
                        : spec.allowed_models.filter((value) => value !== model.id),
                    })
                  }
                />
                {model.name}
              </label>
            ))}
          </div>
        </div>
        <label className="text-n600 block text-xs">
          {t("reasoning")}
          <select
            value={spec.reasoning ?? ""}
            onChange={(event) => onChange({ ...spec, reasoning: event.target.value || null })}
            className={`${INPUT} mt-1.5`}
          >
            <option value="">{t("followDefault")}</option>
            {unsupportedReasoning && (
              <option value={spec.reasoning!} disabled>
                {spec.reasoning}
              </option>
            )}
            {variants.map((variant) => (
              <option key={variant} value={variant}>
                {variant}
              </option>
            ))}
          </select>
          {unsupportedReasoning && (
            <p className="text-danger mt-2" role="alert">
              {t("unsupportedReasoning")}
            </p>
          )}
          {capabilities.error && (
            <p className="text-danger mt-2" role="alert">
              {capabilities.error.message}
            </p>
          )}
        </label>
        <Field
          label={t("exampleTasks")}
          value={spec.example_tasks.join("\n")}
          multiline
          onChange={(value) =>
            onChange({ ...spec, example_tasks: value.split("\n").filter(Boolean).slice(0, 5) })
          }
        />
        <div className="grid grid-cols-2 gap-3">
          <Field
            label={t("maxSteps")}
            type="number"
            min={1}
            max={500}
            value={spec.execution_policy.max_steps}
            onChange={(value) =>
              onChange({ ...spec, execution_policy: { ...spec.execution_policy, max_steps: Number(value) } })
            }
          />
          <Field
            label={t("maxSeconds")}
            type="number"
            min={10}
            max={86400}
            value={spec.execution_policy.max_wall_time_seconds}
            onChange={(value) =>
              onChange({
                ...spec,
                execution_policy: { ...spec.execution_policy, max_wall_time_seconds: Number(value) },
              })
            }
          />
        </div>
        <p className="text-n600 text-xs">{t("agentTimeLimitHint")}</p>
        <fieldset className="space-y-2">
          <legend className="text-n600 text-xs">{t("toolCategories")}</legend>
          <p className="text-n600 text-xs">{t("toolCategoriesHint")}</p>
          <div className="flex flex-wrap gap-3">
            {TOOL_CATEGORIES.map((category) => (
              <label key={category} className="flex items-center gap-1.5 text-xs">
                <input
                  type="checkbox"
                  checked={spec.execution_policy.tool_categories.includes(category)}
                  onChange={(event) =>
                    onChange({
                      ...spec,
                      execution_policy: {
                        ...spec.execution_policy,
                        tool_categories: event.target.checked
                          ? [...spec.execution_policy.tool_categories, category]
                          : spec.execution_policy.tool_categories.filter((value) => value !== category),
                      },
                    })
                  }
                  className="accent-accent"
                />
                {t(`toolCategory.${category}`)}
              </label>
            ))}
          </div>
        </fieldset>
        <JsonField
          label={t("inputSchema")}
          value={spec.input_schema}
          onChange={(value) => onChange({ ...spec, input_schema: value as AgentSpec["input_schema"] })}
        />
        <JsonField
          label={t("outputSchema")}
          value={spec.output_schema}
          onChange={(value) => onChange({ ...spec, output_schema: value as AgentSpec["output_schema"] })}
        />
        <JsonField
          label={t("generationOptions")}
          value={spec.generation_options}
          onChange={(value) =>
            onChange({ ...spec, generation_options: (value ?? {}) as Record<string, unknown> })
          }
        />
        <p className="text-n600 text-xs">{t("generationOptionsHint")}</p>
        <Field
          label={t("resources")}
          value={spec.resource_refs.join("\n")}
          multiline
          onChange={(value) => onChange({ ...spec, resource_refs: value.split("\n").filter(Boolean) })}
        />
      </div>
    </details>
  )
}
export function AgentForm({
  spec,
  onChange,
  models,
  onOptimize,
}: {
  spec: AgentSpec
  onChange: (spec: AgentSpec) => void
  models: ModelInfo[]
  onOptimize?: (field: "instruction" | "when_to_use") => void
}) {
  const { t } = useTranslation("teams")
  return (
    <div className="space-y-4">
      <FormSection title={t("basicInfo")}>
        <Field
          label={t("name")}
          value={spec.name}
          onChange={(name) => onChange({ ...spec, name })}
          maxLength={40}
          required
        />
        <Field
          label={t("description")}
          value={spec.description}
          onChange={(description) => onChange({ ...spec, description })}
          maxLength={500}
          required
        />
        <Field
          label={t("whenToUse")}
          value={spec.when_to_use}
          onChange={(when_to_use) => onChange({ ...spec, when_to_use })}
          multiline
          maxLength={1000}
          required
        />
        {onOptimize && (
          <button type="button" onClick={() => onOptimize("when_to_use")} className={`${BUTTON} self-start`}>
            {t("optimizeUsage")}
          </button>
        )}
        <Field
          label={t("instruction")}
          value={spec.instruction}
          onChange={(instruction) => onChange({ ...spec, instruction })}
          multiline
          maxLength={8192}
          required
        />
        {onOptimize && (
          <button type="button" onClick={() => onOptimize("instruction")} className={`${BUTTON} self-start`}>
            {t("optimizeInstruction")}
          </button>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field
            label={t("icon")}
            value={spec.display.icon}
            onChange={(icon) => onChange({ ...spec, display: { ...spec.display, icon } })}
            maxLength={64}
          />
          <Field
            label={t("color")}
            value={spec.display.color}
            onChange={(color) => onChange({ ...spec, display: { ...spec.display, color } })}
            maxLength={32}
          />
        </div>
      </FormSection>
      <SkillsAndTools spec={spec} onChange={onChange} />
      <Advanced spec={spec} onChange={onChange} models={models} />
    </div>
  )
}

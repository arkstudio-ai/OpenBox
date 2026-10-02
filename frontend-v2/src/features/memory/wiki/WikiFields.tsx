import { useTranslation } from "react-i18next"
import { memoryInput } from "@/shared/ui/MemoryDiagnostics"
import type { FieldDefinition } from "./workflow-api"
import { Field } from "./platform-ui"

export function WikiFields({
  definitions,
  values,
  onChange,
  omit = [],
}: {
  definitions: Record<string, FieldDefinition>
  values: Record<string, unknown>
  onChange: (values: Record<string, unknown>) => void
  omit?: string[]
}) {
  const { t } = useTranslation("wiki")
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {Object.entries(definitions)
        .filter(([key]) => !omit.includes(key))
        .map(([key, definition]) => {
          const value = values[key] ?? definition.default ?? ""
          const update = (next: unknown) => onChange({ ...values, [key]: next })
          if (definition.type === "boolean")
            return (
              <label key={key} className="flex gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={value === true}
                  onChange={(event) => update(event.target.checked)}
                />
                {key}
              </label>
            )
          if (definition.type === "enum")
            return (
              <Field key={key} label={key + (definition.required ? " *" : "")}>
                <select
                  required={definition.required}
                  className={memoryInput}
                  value={String(value)}
                  onChange={(event) => update(event.target.value)}
                >
                  <option value="">{t("choose")}</option>
                  {definition.enum?.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </Field>
            )
          if (definition.type.endsWith("[]"))
            return (
              <Field key={key} label={key + " · " + t("onePerLine")}>
                <textarea
                  className={memoryInput}
                  value={Array.isArray(value) ? value.join("\n") : ""}
                  onChange={(event) => update(event.target.value.split("\n").filter(Boolean))}
                />
              </Field>
            )
          const numeric = ["integer", "number"].includes(definition.type)
          return (
            <Field key={key} label={key + (definition.required ? " *" : "")}>
              <input
                required={definition.required}
                className={memoryInput}
                type={numeric ? "number" : definition.type === "date" ? "date" : "text"}
                min={numeric ? definition.min : undefined}
                max={numeric ? definition.max : undefined}
                maxLength={!numeric ? (definition.max ?? 8000) : undefined}
                step={definition.type === "integer" ? 1 : "any"}
                value={String(value)}
                onChange={(event) => update(numeric ? Number(event.target.value) : event.target.value)}
              />
            </Field>
          )
        })}
    </div>
  )
}

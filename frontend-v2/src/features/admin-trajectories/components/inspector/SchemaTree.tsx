import { useTranslation } from "react-i18next"
import { isPlainObject, type PlainObject } from "../../utils/python"
import { JsonTree } from "./JsonTree"
import { NS } from "./types"

interface SchemaTreeProps {
  schema: unknown
}

interface SchemaNodeProps {
  name: string | null
  schema: PlainObject
  required: boolean
  depth: number
}

const MAX_DEPTH = 8
const COMBINATORS = ["anyOf", "oneOf", "allOf"] as const
const CONSTRAINTS = [
  "format",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "minLength",
  "maxLength",
  "pattern",
  "minItems",
  "maxItems",
  "uniqueItems",
  "const",
] as const

function typeText(schema: PlainObject): string | null {
  const type = schema.type
  if (Array.isArray(type)) return type.join(" | ")
  if (typeof type === "string") return type
  if (typeof schema.$ref === "string") return schema.$ref
  return null
}

function SchemaNode({ name, schema, required, depth }: SchemaNodeProps) {
  const { t } = useTranslation(NS)
  const properties = isPlainObject(schema.properties) ? schema.properties : null
  const requiredList = Array.isArray(schema.required)
    ? schema.required.filter((item) => typeof item === "string")
    : []
  const type = typeText(schema)
  const constraints = CONSTRAINTS.filter((key) => key in schema)
  return (
    <div className="flex min-w-0 flex-col gap-1 py-1">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        {name !== null && <span className="text-ink font-mono text-xs font-medium">{name}</span>}
        {type && <span className="text-a700 text-2xs font-mono">{type}</span>}
        {required && <span className="text-dangerink text-2xs">{t("schema.required")}</span>}
        {constraints.map((key) => (
          <span key={key} className="bg-hairsoft text-n700 text-2xs rounded px-1 font-mono">
            {key}={JSON.stringify(schema[key])}
          </span>
        ))}
      </div>
      {typeof schema.description === "string" && (
        <p className="text-n700 text-xs whitespace-pre-wrap">{schema.description}</p>
      )}
      {Array.isArray(schema.enum) && (
        <p className="text-n600 text-2xs font-mono">
          {t("schema.enum", { values: schema.enum.map((item) => JSON.stringify(item)).join(", ") })}
        </p>
      )}
      {"default" in schema && (
        <p className="text-n600 text-2xs font-mono">
          {t("schema.default", { value: JSON.stringify(schema.default) })}
        </p>
      )}
      {depth < MAX_DEPTH && (
        <div className="border-hair ms-1 border-s ps-3">
          {properties &&
            Object.entries(properties).map(([key, child]) =>
              isPlainObject(child) ? (
                <SchemaNode
                  key={key}
                  name={key}
                  schema={child}
                  required={requiredList.includes(key)}
                  depth={depth + 1}
                />
              ) : null,
            )}
          {isPlainObject(schema.items) && (
            <SchemaNode name={t("schema.items")} schema={schema.items} required={false} depth={depth + 1} />
          )}
          {isPlainObject(schema.additionalProperties) && (
            <SchemaNode
              name={t("schema.additional")}
              schema={schema.additionalProperties}
              required={false}
              depth={depth + 1}
            />
          )}
          {COMBINATORS.map((key) =>
            Array.isArray(schema[key])
              ? (schema[key] as unknown[]).map((option, index) =>
                  isPlainObject(option) ? (
                    <SchemaNode
                      key={`${key}-${index}`}
                      name={t("schema.option", { kind: key, index: index + 1 })}
                      schema={option}
                      required={false}
                      depth={depth + 1}
                    />
                  ) : null,
                )
              : null,
          )}
        </div>
      )}
    </div>
  )
}

/** A JSON Schema as defined — types, requirements and constraints — never inferred from argument values. */
export function SchemaTree({ schema }: SchemaTreeProps) {
  if (!isPlainObject(schema)) return <JsonTree value={schema} />
  return (
    <div
      className="border-hair bg-surface max-h-[36rem] overflow-auto rounded-lg border px-3 py-2"
      data-testid="trajectory-schema-tree"
    >
      <SchemaNode name={null} schema={schema} required={false} depth={0} />
    </div>
  )
}

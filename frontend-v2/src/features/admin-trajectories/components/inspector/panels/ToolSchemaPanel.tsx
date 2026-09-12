import { useTranslation } from "react-i18next"
import { labelKey, SCHEMA_SOURCE_LABELS } from "../../../constants/labels"
import { fieldState } from "../../../utils/availability"
import { isPlainObject, type PlainObject } from "../../../utils/python"
import { ContentActions } from "../ContentActions"
import { Field, FieldList, Section } from "../Field"
import { RecordLink } from "../RecordLink"
import { SchemaTree } from "../SchemaTree"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

const SOURCE_NOTES: Readonly<Record<string, string>> = {
  provider_request: "schema.providerNote",
  executor_registry: "schema.registryNote",
}

/**
 * The definition in force for this call and where it came from. The copy sent
 * to the model and the executor's own registration are labelled differently;
 * nothing is inferred from the argument values.
 */
export function ToolSchemaPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const data = record.data ?? {}
  const schema = fieldState(record, "schema")
  const source = typeof data.schema_source === "string" ? data.schema_source : null
  const reference = typeof data.request_schema_ref === "string" ? data.request_schema_ref : null
  const outer: PlainObject | null =
    schema.state === "available" && isPlainObject(schema.value) ? schema.value : null
  const definition: PlainObject | null = outer && isPlainObject(outer.function) ? outer.function : outer
  const parameters = definition
    ? (definition.parameters ?? definition.input_schema ?? definition.inputSchema)
    : undefined
  const output = definition ? (definition.output_schema ?? definition.outputSchema) : undefined
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-tool-schema">
      <div className="bg-hairsoft flex flex-col gap-1 rounded-lg px-3 py-2 text-xs">
        <span className="text-ink font-medium">
          {source
            ? t(labelKey(SCHEMA_SOURCE_LABELS, source, "schemaSource.other"), { value: source })
            : t("schemaSource.unknown")}
        </span>
        {source && SOURCE_NOTES[source] && <span className="text-n600">{t(SOURCE_NOTES[source])}</span>}
        {reference && (
          <RecordLink recordId={`request:${reference}`} label={t("schema.fromRequest", { id: reference })} />
        )}
      </div>
      {!definition ? (
        <ValueView field={schema} />
      ) : (
        <>
          <FieldList>
            <Field label={t("field.name")}>
              {typeof definition.name === "string" ? definition.name : t("common.dash")}
            </Field>
            {typeof definition.description === "string" && (
              <Field label={t("schema.description")}>
                <span className="whitespace-pre-wrap">{definition.description}</span>
              </Field>
            )}
          </FieldList>
          <Section
            title={t("schema.parameters")}
            actions={<ContentActions value={outer} name={`${record.record_id}-schema`} format="json" />}
          >
            {parameters !== undefined ? (
              <SchemaTree schema={parameters} />
            ) : (
              <ValueView field={{ state: "not_recorded" }} />
            )}
          </Section>
          {output !== undefined && (
            <Section title={t("schema.output")}>
              <SchemaTree schema={output} />
            </Section>
          )}
        </>
      )}
    </div>
  )
}

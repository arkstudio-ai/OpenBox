import { useTranslation } from "react-i18next"
import { isPlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { Section } from "../Field"
import { ToolDefinitionList } from "../ToolDefinitionList"
import { NS, type PanelProps } from "../types"

function catalogOf(data: Record<string, unknown>): unknown {
  if ("tools" in data) return data.tools
  if (isPlainObject(data.after) && "tools" in data.after) return data.after.tools
  return undefined
}

/** The complete tool catalog visible to the model at this state, including tools never called. */
export function ToolCatalogPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const tools = catalogOf(record.data ?? {})
  return (
    <Section
      title={t("tools.catalog", { count: Array.isArray(tools) ? tools.length : 0 })}
      actions={
        Array.isArray(tools) ? (
          <ContentActions value={tools} name={`${record.record_id}-tools`} format="json" />
        ) : undefined
      }
    >
      {tools === undefined && <AvailabilityNote state="not_recorded" />}
      {tools !== undefined && !Array.isArray(tools) && <AvailabilityNote state="empty" />}
      {Array.isArray(tools) &&
        (tools.length ? <ToolDefinitionList tools={tools} /> : <AvailabilityNote state="empty" />)}
    </Section>
  )
}

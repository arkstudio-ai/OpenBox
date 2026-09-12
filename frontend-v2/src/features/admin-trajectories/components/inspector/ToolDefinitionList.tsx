import { useState } from "react"
import { useTranslation } from "react-i18next"
import { isPlainObject, type PlainObject } from "../../utils/python"
import { JsonTree } from "./JsonTree"
import { SchemaTree } from "./SchemaTree"
import { NS } from "./types"

interface ToolDefinitionListProps {
  tools: readonly unknown[]
}

const FILTER_FROM = 12

function definitionOf(tool: unknown): {
  name: string | null
  description: string | null
  parameters: unknown
  raw: unknown
} {
  const outer: PlainObject = isPlainObject(tool) ? tool : {}
  const inner: PlainObject = isPlainObject(outer.function) ? outer.function : outer
  return {
    name: typeof inner.name === "string" ? inner.name : null,
    description: typeof inner.description === "string" ? inner.description : null,
    parameters: inner.parameters ?? inner.input_schema ?? inner.inputSchema,
    raw: tool,
  }
}

/** Every tool definition the model could see, including ones never called. */
export function ToolDefinitionList({ tools }: ToolDefinitionListProps) {
  const { t } = useTranslation(NS)
  const [query, setQuery] = useState("")
  const needle = query.trim().toLocaleLowerCase()
  const definitions = tools.map(definitionOf)
  const shown = needle
    ? definitions.filter((item) => (item.name ?? "").toLocaleLowerCase().includes(needle))
    : definitions
  return (
    <div className="flex flex-col gap-2">
      {tools.length >= FILTER_FROM && (
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("tools.filterPlaceholder")}
          aria-label={t("tools.filterPlaceholder")}
          className="border-hair bg-bg text-ink placeholder:text-n500 rounded-lg border px-2.5 py-1 text-xs"
        />
      )}
      <ul className="flex flex-col gap-1.5">
        {shown.map((item, index) => (
          <li key={`${item.name ?? "tool"}-${index}`}>
            <details className="border-hair rounded-lg border px-3 py-1.5">
              <summary className="cursor-pointer text-xs">
                <span className="text-ink font-mono font-medium">{item.name ?? t("tools.unnamed")}</span>
                {item.description && (
                  <span className="text-n600 ms-2 line-clamp-1 inline">{item.description}</span>
                )}
              </summary>
              <div className="mt-2 flex flex-col gap-2">
                {item.description && (
                  <p className="text-n700 text-xs whitespace-pre-wrap">{item.description}</p>
                )}
                {item.parameters !== undefined ? (
                  <SchemaTree schema={item.parameters} />
                ) : (
                  <JsonTree value={item.raw} openDepth={1} />
                )}
              </div>
            </details>
          </li>
        ))}
      </ul>
    </div>
  )
}

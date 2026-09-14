import { useTranslation } from "react-i18next"
import { stableText } from "../../../utils/diff"
import { isPlainObject } from "../../../utils/python"
import { DiffView } from "../DiffView"
import { Section } from "../Field"
import { RecordLink } from "../RecordLink"
import { NS, type PanelProps } from "../types"

interface ToolChanges {
  added: string[]
  removed: string[]
  changed: Array<{ name: string; before: string; after: string }>
}

function byName(tools: unknown): Map<string, string> {
  const map = new Map<string, string>()
  if (!Array.isArray(tools)) return map
  tools.forEach((tool, index) => {
    const inner = isPlainObject(tool) && isPlainObject(tool.function) ? tool.function : tool
    const name = isPlainObject(inner) && typeof inner.name === "string" ? inner.name : `#${index + 1}`
    map.set(name, stableText(tool))
  })
  return map
}

function compareTools(before: unknown, after: unknown): ToolChanges {
  const old = byName(before)
  const next = byName(after)
  return {
    added: [...next.keys()].filter((name) => !old.has(name)),
    removed: [...old.keys()].filter((name) => !next.has(name)),
    changed: [...next.entries()].flatMap(([name, text]) => {
      const previous = old.get(name)
      return previous !== undefined && previous !== text ? [{ name, before: previous, after: text }] : []
    }),
  }
}

/** What changed from the previous system state of the same agent: prompt text and tool definitions. */
export function SystemDiffPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const data = record.data ?? {}
  const before = isPlainObject(data.before) ? data.before : null
  const source = typeof data.source_request_id === "string" ? data.source_request_id : null
  if (!before) return <p className="text-n600 text-xs">{t("system.initial")}</p>
  const tools = compareTools(before.tools, data.tools)
  const systemChanged = stableText(before.system ?? "") !== stableText(data.system ?? "")
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-system-diff">
      {source && (
        <RecordLink recordId={`request:${source}`} label={t("system.effectiveFrom", { id: source })} />
      )}
      <Section title={t("system.promptChanges")}>
        {systemChanged ? (
          <DiffView before={stableText(before.system ?? "")} after={stableText(data.system ?? "")} />
        ) : (
          <p className="text-n500 text-xs">{t("diff.identical")}</p>
        )}
      </Section>
      <Section title={t("system.toolChanges")}>
        {!tools.added.length && !tools.removed.length && !tools.changed.length && (
          <p className="text-n500 text-xs">{t("diff.identical")}</p>
        )}
        {tools.added.length > 0 && (
          <p className="text-xs">{t("system.toolsAdded", { names: tools.added.join(", ") })}</p>
        )}
        {tools.removed.length > 0 && (
          <p className="text-xs">{t("system.toolsRemoved", { names: tools.removed.join(", ") })}</p>
        )}
        {tools.changed.map((change) => (
          <details key={change.name} className="border-hair rounded-lg border px-3 py-1.5">
            <summary className="cursor-pointer font-mono text-xs">
              {t("system.toolChanged", { name: change.name })}
            </summary>
            <div className="mt-2">
              <DiffView before={change.before} after={change.after} />
            </div>
          </details>
        ))}
      </Section>
    </div>
  )
}

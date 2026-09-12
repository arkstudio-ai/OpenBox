import { useState } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { CopyButton } from "./CopyButton"
import { useEnvelopeRenderer } from "./envelopeContext"
import { isContentEnvelope } from "./media"

interface JsonTreeProps {
  value: unknown
  /** Levels expanded on first render. */
  openDepth?: number
}

interface NodeProps {
  name: string | null
  value: unknown
  path: string
  depth: number
  openDepth: number
}

interface LeafProps {
  value: unknown
}

const PAGE = 200
const LONG_STRING = 2_000

function childPath(path: string, key: string | number): string {
  if (typeof key === "number") return `${path}[${key}]`
  return /^[A-Za-z_$][\w$]*$/.test(key) ? `${path}.${key}` : `${path}[${JSON.stringify(key)}]`
}

function Leaf({ value }: LeafProps) {
  const { t } = useTranslation("admin-trajectories")
  const [expanded, setExpanded] = useState(false)
  if (typeof value === "string") {
    const long = value.length > LONG_STRING && !expanded
    return (
      <span className="text-s700 break-all whitespace-pre-wrap">
        {JSON.stringify(long ? value.slice(0, LONG_STRING) : value)}
        {long && (
          <button type="button" className="text-a700 ms-1 hover:underline" onClick={() => setExpanded(true)}>
            {t("common.showAll", { count: value.length })}
          </button>
        )}
      </span>
    )
  }
  const tone = value === null ? "text-n500" : typeof value === "number" ? "text-a700" : "text-n800"
  return <span className={tone}>{JSON.stringify(value)}</span>
}

function JsonNode({ name, value, path, depth, openDepth }: NodeProps) {
  const { t } = useTranslation("admin-trajectories")
  const renderEnvelope = useEnvelopeRenderer()
  const [limit, setLimit] = useState(PAGE)
  const label = name === null ? null : <span className="text-n600">{name}: </span>
  if (renderEnvelope && isContentEnvelope(value)) {
    return (
      <div className="flex min-w-0 flex-col gap-1 py-0.5 font-sans">
        <span className="flex items-center gap-1 font-mono">
          {label}
          <CopyButton text={path} label={t("common.copyPath")} className="size-5" />
        </span>
        {renderEnvelope(value)}
      </div>
    )
  }
  if (value === null || typeof value !== "object") {
    return (
      <div className="group flex min-w-0 items-start gap-1 py-px">
        <span className="min-w-0 flex-1">
          {label}
          <Leaf value={value} />
        </span>
        <CopyButton
          text={path}
          label={t("common.copyPath")}
          className="size-5 opacity-0 group-hover:opacity-100 focus:opacity-100"
        />
      </div>
    )
  }
  const entries: Array<[string | number, unknown]> = Array.isArray(value)
    ? value.map((item, index) => [index, item])
    : Object.entries(value)
  const summary = Array.isArray(value)
    ? t("json.items", { count: entries.length })
    : t("json.keys", { count: entries.length })
  return (
    <details open={depth < openDepth} className="min-w-0">
      <summary className="group hover:bg-hairsoft flex cursor-pointer list-none items-center gap-1 rounded py-px">
        <span className="min-w-0 flex-1 truncate">
          {label}
          <span className="text-n500">{summary}</span>
        </span>
        <CopyButton
          text={JSON.stringify(value, null, 2)}
          label={t("common.copyValue")}
          className="size-5 opacity-0 group-hover:opacity-100 focus:opacity-100"
        />
        <CopyButton
          text={path}
          label={t("common.copyPath")}
          className="size-5 opacity-0 group-hover:opacity-100 focus:opacity-100"
        />
      </summary>
      <div className="border-hair ms-2 border-s ps-3">
        {entries.slice(0, limit).map(([key, item]) => (
          <JsonNode
            key={String(key)}
            name={String(key)}
            value={item}
            path={childPath(path, key)}
            depth={depth + 1}
            openDepth={openDepth}
          />
        ))}
        {entries.length > limit && (
          <button
            type="button"
            className="text-a700 py-1 text-xs hover:underline"
            onClick={() => setLimit((current) => current + PAGE)}
          >
            {t("json.more", { count: entries.length - limit })}
          </button>
        )}
      </div>
    </details>
  )
}

/**
 * Read-only JSON tree with copy for any value or path. Retained-content
 * wrappers inside it render through the inspector's protected viewer; plain
 * URLs and data stay inert text.
 */
export function JsonTree({ value, openDepth = 2 }: JsonTreeProps) {
  return (
    <div
      className={cn(
        "border-hair bg-surface max-h-[36rem] overflow-auto rounded-lg border p-2 font-mono text-xs",
      )}
    >
      <JsonNode name={null} value={value} path="$" depth={0} openDepth={openDepth} />
    </div>
  )
}

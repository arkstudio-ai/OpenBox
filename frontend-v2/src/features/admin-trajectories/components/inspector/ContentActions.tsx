import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { Download } from "lucide-react"
import { saveBlob } from "../../utils/download"
import { CopyButton } from "./CopyButton"
import { NS } from "./types"

export type ContentFormat = "json" | "text" | "markdown" | "jsonl"

interface ContentActionsProps {
  value: unknown
  /** File name stem; unsafe characters are replaced. */
  name: string
  format: ContentFormat
}

const EXTENSIONS: Readonly<Record<ContentFormat, [string, string]>> = {
  json: ["json", "application/json"],
  jsonl: ["jsonl", "application/x-ndjson"],
  text: ["txt", "text/plain"],
  markdown: ["md", "text/markdown"],
}

function serialise(value: unknown, format: ContentFormat): string {
  if (format === "json") return JSON.stringify(value, null, 2) ?? ""
  if (format === "jsonl" && Array.isArray(value)) return value.map((item) => JSON.stringify(item)).join("\n")
  return typeof value === "string" ? value : (JSON.stringify(value, null, 2) ?? "")
}

/**
 * Copy or save a captured value as the admin sees it. The file is built from
 * the value already on screen; nothing is fetched and nothing is executed.
 */
export function ContentActions({ value, name, format }: ContentActionsProps) {
  const { t } = useTranslation(NS)
  const text = useMemo(() => serialise(value, format), [value, format])
  const [extension, mediaType] = EXTENSIONS[format]
  const filename = `${name.replace(/[^\w.-]+/g, "_").slice(0, 120) || "trajectory"}.${extension}`
  return (
    <span className="inline-flex flex-none items-center gap-0.5">
      <CopyButton
        text={text}
        label={t(format === "text" || format === "markdown" ? "common.copyText" : "common.copyJson")}
      />
      <button
        type="button"
        onClick={() => saveBlob(new Blob([text], { type: mediaType }), filename)}
        aria-label={t("common.download", { name: filename })}
        title={t("common.download", { name: filename })}
        className="text-n500 hover:text-ink hover:bg-hairsoft inline-flex size-6 items-center justify-center rounded"
      >
        <Download size={12} aria-hidden />
      </button>
    </span>
  )
}

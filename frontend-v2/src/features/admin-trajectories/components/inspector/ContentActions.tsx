import { useContext, useMemo, useState, type MouseEvent } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Check, Copy, Download } from "lucide-react"
import { useCopy } from "@/shared/hooks/useCopy"
import { Spinner } from "@/shared/ui/Spinner"
import { trackRequest } from "../../api/access"
import { useAccessScope } from "../../api/queries"
import { containsRefs, resolveValue } from "../../api/refs"
import { saveBlob } from "../../utils/download"
import { InspectorContext, useInspector } from "./context"
import { CopyButton } from "./CopyButton"
import { NS } from "./types"

export type ContentFormat = "json" | "text" | "markdown" | "jsonl"

interface ContentActionsProps {
  value: unknown
  /** File name stem; unsafe characters are replaced. */
  name: string
  format: ContentFormat
}

interface ReadingActionsProps {
  value: unknown
  format: ContentFormat
  filename: string
}

const EXTENSIONS: Readonly<Record<ContentFormat, [string, string]>> = {
  json: ["json", "application/json"],
  jsonl: ["jsonl", "application/x-ndjson"],
  text: ["txt", "text/plain"],
  markdown: ["md", "text/markdown"],
}

const ACTION =
  "text-n500 hover:text-ink hover:bg-hairsoft inline-flex size-6 items-center justify-center rounded"

function serialise(value: unknown, format: ContentFormat): string {
  if (format === "json") return JSON.stringify(value, null, 2) ?? ""
  if (format === "jsonl" && Array.isArray(value)) return value.map((item) => JSON.stringify(item)).join("\n")
  return typeof value === "string" ? value : (JSON.stringify(value, null, 2) ?? "")
}

function copyLabel(format: ContentFormat): string {
  return format === "text" || format === "markdown" ? "common.copyText" : "common.copyJson"
}

/**
 * Copy and save for a value that still holds `$ref` envelopes. The references
 * are read first — through the digest cache the panels share — and nothing
 * reaches the clipboard or a file if access changed while they were read.
 */
function ReadingActions({ value, format, filename }: ReadingActionsProps) {
  const { t } = useTranslation(NS)
  const client = useQueryClient()
  const { viewer } = useAccessScope()
  const { sessionId, throughSeq } = useInspector()
  const { copied, copy } = useCopy()
  const [state, setState] = useState<"idle" | "reading" | "failed">("idle")
  const reading = state === "reading"
  const [, mediaType] = EXTENSIONS[format]

  const act = (deliver: (text: string) => void) => async (event: MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    const request = trackRequest()
    setState("reading")
    try {
      request.check()
      const resolved = await resolveValue(client, { viewer, sessionId, throughSeq }, value)
      request.check()
      deliver(serialise(resolved, format))
      setState("idle")
    } catch {
      setState("failed")
    } finally {
      request.release()
    }
  }

  const copyText = t(copyLabel(format))
  const saveText = t("common.download", { name: filename })
  const copyIcon = copied ? <Check size={12} aria-hidden /> : <Copy size={12} aria-hidden />
  return (
    <span className="inline-flex flex-none items-center gap-0.5" aria-busy={reading}>
      <button
        type="button"
        onClick={act(copy)}
        disabled={reading}
        aria-label={copyText}
        title={reading ? t("content.preparing") : copied ? t("common.copied") : copyText}
        className={ACTION}
      >
        {reading ? <Spinner className="size-3" /> : copyIcon}
      </button>
      <button
        type="button"
        onClick={act((text) => saveBlob(new Blob([text], { type: mediaType }), filename))}
        disabled={reading}
        aria-label={saveText}
        title={saveText}
        className={ACTION}
      >
        <Download size={12} aria-hidden />
      </button>
      {state === "failed" && (
        <span role="alert" className="text-dangerink text-2xs">
          {t("content.actionFailed")}
        </span>
      )}
    </span>
  )
}

/**
 * Copy or save a captured value as the admin sees it; nothing is executed. The
 * file is built from the value on screen — or, when that value still holds
 * `$ref` envelopes, from it once its references were read (SPEC §11.2).
 */
export function ContentActions({ value, name, format }: ContentActionsProps) {
  const { t } = useTranslation(NS)
  // Only a server with `capabilities.refs` leaves `$ref` envelopes in a detail;
  // for any other server a look-alike is captured data and is copied as it is.
  const refs = useContext(InspectorContext)?.refs === true
  const referenced = useMemo(() => refs && containsRefs(value), [refs, value])
  const text = useMemo(() => (referenced ? "" : serialise(value, format)), [format, referenced, value])
  const [extension, mediaType] = EXTENSIONS[format]
  const filename = `${name.replace(/[^\w.-]+/g, "_").slice(0, 120) || "trajectory"}.${extension}`
  if (referenced) return <ReadingActions value={value} format={format} filename={filename} />
  return (
    <span className="inline-flex flex-none items-center gap-0.5">
      <CopyButton text={text} label={t(copyLabel(format))} />
      <button
        type="button"
        onClick={() => saveBlob(new Blob([text], { type: mediaType }), filename)}
        aria-label={t("common.download", { name: filename })}
        title={t("common.download", { name: filename })}
        className={ACTION}
      >
        <Download size={12} aria-hidden />
      </button>
    </span>
  )
}

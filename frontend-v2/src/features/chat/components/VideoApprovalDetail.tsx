import { useTranslation } from "react-i18next"
import type { QuestionItem } from "@/shared/types/api"

type UnknownRecord = Record<string, unknown>

interface VideoSegmentDetail {
  ordinal: number
  role: string
  scriptText: string
  prompt: string
}

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null
}

function firstText(record: UnknownRecord | null, keys: string[]): string {
  if (!record) return ""
  for (const key of keys) {
    const value = record[key]
    if (typeof value === "string" && value.trim()) return value
  }
  return ""
}

function readScript(detail: UnknownRecord): string {
  const direct = firstText(detail, ["script_text", "scriptText", "content"])
  if (direct) return direct
  return firstText(asRecord(detail.script), ["text", "script_text", "content"])
}

function readSegments(detail: UnknownRecord): VideoSegmentDetail[] {
  if (!Array.isArray(detail.segments)) return []

  return detail.segments.flatMap((value, index) => {
    const segment = asRecord(value)
    if (!segment) return []

    const rawOrdinal = segment.ordinal ?? segment.index ?? segment.number
    const ordinal =
      typeof rawOrdinal === "number" && Number.isFinite(rawOrdinal)
        ? rawOrdinal
        : typeof rawOrdinal === "string" && /^\d+$/.test(rawOrdinal)
          ? Number(rawOrdinal)
          : index + 1
    const scriptText = firstText(segment, [
      "script_text",
      "scriptText",
      "transcript",
      "dialogue",
      "content",
    ])
    const prompt = firstText(segment, ["prompt", "segment_prompt", "segmentPrompt"])

    if (!scriptText && !prompt) return []
    return [
      {
        ordinal,
        role: firstText(segment, ["role", "speaker"]),
        scriptText,
        prompt,
      },
    ]
  })
}

function promptSummary(prompt: string): string {
  const compact = prompt.replace(/\s+/g, " ").trim()
  return compact.length > 88 ? `${compact.slice(0, 88)}…` : compact
}

/** Full evidence for video script/segment approvals. Unknown question details
 * intentionally render nothing so existing stored requests remain compatible. */
export function VideoApprovalDetail({ item }: { item: QuestionItem }) {
  const { t } = useTranslation("chat")
  const detail = asRecord(item.detail)
  if (!detail) return null

  const kind = firstText(detail, ["kind", "type"])
  const script = readScript(detail)
  const segments = readSegments(detail)
  const isVideoDetail =
    kind === "video_script_approval" ||
    kind === "video_segments_approval" ||
    "script_text" in detail ||
    Array.isArray(detail.segments)

  if (!isVideoDetail) return null

  const roleLabel = (role: string) => {
    if (["hook", "body", "transition", "closing"].includes(role)) {
      return t(`question.videoApproval.roles.${role}`)
    }
    return role || t("question.videoApproval.roles.unknown")
  }

  return (
    <div className="border-hair bg-bg max-h-[min(52vh,32rem)] overflow-y-auto overscroll-contain rounded-lg border p-3 pr-2">
      {script && (
        <section className="space-y-1.5">
          <h3 className="text-ink text-sm font-medium">{t("question.videoApproval.fullScript")}</h3>
          <p className="text-n700 whitespace-pre-wrap break-words text-sm leading-6">{script}</p>
        </section>
      )}

      {segments.length > 0 && (
        <div className="space-y-2.5">
          {segments.map((segment) => (
            <section
              key={`${segment.ordinal}:${segment.role}:${segment.scriptText}`}
              className="border-hair bg-card rounded-lg border p-3"
            >
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <h3 className="text-ink text-sm font-medium">
                  {t("question.videoApproval.segment", { number: segment.ordinal })}
                </h3>
                <span className="bg-hairsoft text-n700 rounded-full px-2 py-0.5 text-xs">
                  {t("question.videoApproval.role", { role: roleLabel(segment.role) })}
                </span>
              </div>

              {segment.scriptText && (
                <div>
                  <div className="text-n600 mb-1 text-xs">{t("question.videoApproval.transcript")}</div>
                  <p className="text-ink whitespace-pre-wrap break-words text-sm leading-6">
                    {segment.scriptText}
                  </p>
                </div>
              )}

              {segment.prompt && (
                <details className="border-hair mt-2 border-t pt-2">
                  <summary className="text-n700 hover:text-ink cursor-pointer text-xs leading-5">
                    {t("question.videoApproval.prompt")}: {promptSummary(segment.prompt)}
                  </summary>
                  <p className="text-n700 mt-2 whitespace-pre-wrap break-words text-xs leading-5">
                    {segment.prompt}
                  </p>
                </details>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  )
}

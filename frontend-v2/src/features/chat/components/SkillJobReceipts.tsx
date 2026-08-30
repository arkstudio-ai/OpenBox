import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { AssetPreview } from "@/shared/ui/AssetPreview"
import type { MessagePart, SkillJobPart } from "@/shared/types/api"

function receiptParts(parts: MessagePart[]): SkillJobPart[] {
  return parts.filter((part): part is SkillJobPart => part.type === "skill_job")
}

// Historical receipts render from stored part data alone. Keep this local to
// chat so archived transcripts never depend on a retired runtime or API.
function receiptTone(status: string | undefined): { dot: string; labelKey?: string } {
  switch (status) {
    case "succeeded":
      return { dot: "bg-sage", labelKey: "status.succeeded" }
    case "failed":
      return { dot: "bg-danger", labelKey: "status.failed" }
    case "cancelled":
      return { dot: "bg-n400", labelKey: "status.cancelled" }
    default:
      return { dot: "bg-n400" }
  }
}

function ReceiptChip({ part }: { part: SkillJobPart }) {
  const { t: tJobs } = useTranslation("jobs")
  const { t: tCommon } = useTranslation("common")
  const status = part.status?.trim() ?? ""
  const tone = receiptTone(status)
  const name = part.skillKey?.replace(/^(builtin|user):/, "").trim() ?? ""
  const operation = part.operation?.trim() ?? ""
  const title = [name, operation].filter(Boolean).join(" · ")
  const statusLabel = tone.labelKey ? tJobs(tone.labelKey) : status || tCommon("state.unavailable")
  return (
    <div className="border-hair bg-n100/50 flex min-w-0 items-center gap-2 rounded-lg border px-3 py-2">
      <span className={cn("size-2 shrink-0 rounded-full", tone.dot)} />
      {title ? <span className="text-n800 shrink-0 text-sm font-medium">{title}</span> : null}
      <span className="text-n500 shrink-0 text-xs">{statusLabel}</span>
      {part.summary ? <span className="text-n600 min-w-0 truncate text-xs">{part.summary}</span> : null}
    </div>
  )
}

function Receipt({ part }: { part: SkillJobPart }) {
  return (
    <div className="min-w-0">
      <ReceiptChip part={part} />
      {/* The transcript is the lasting historical record, so the produced file
          belongs here rather than being reduced to an inert filename. */}
      {(Array.isArray(part.artifacts) ? part.artifacts : []).map((a, index) => (
        <AssetPreview key={`${a.assetId ?? "missing"}-${index}`} artifact={a} />
      ))}
    </div>
  )
}

/** Read-only rendering for durable receipts stored in historical transcripts. */
export function SkillJobReceipts({ parts }: { parts: MessagePart[] }) {
  const receipts = receiptParts(parts)
  if (receipts.length === 0) return null
  return (
    <div className="mt-2 flex flex-col gap-2">
      {receipts.map((part) => (
        <Receipt key={part.id} part={part} />
      ))}
    </div>
  )
}

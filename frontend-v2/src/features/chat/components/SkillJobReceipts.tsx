import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { AssetPreview } from "@/shared/ui/AssetPreview"
import type { MessagePart, SkillJobPart } from "@/shared/types/api"

/** Exported for tests. */
export function receiptParts(parts: MessagePart[]): SkillJobPart[] {
  return parts.filter((part): part is SkillJobPart => part.type === "skill_job")
}

// Local status→tone map: chat renders the receipt from part data alone and
// features must not import each other (ENGINEERING_SPEC §4.1); the jobs
// feature keeps its own richer map for live cards.
function receiptTone(status: string): { dot: string; labelKey: string } {
  switch (status) {
    case "succeeded":
      return { dot: "bg-sage", labelKey: "status.succeeded" }
    case "failed":
      return { dot: "bg-danger", labelKey: "status.failed" }
    case "cancelled":
      return { dot: "bg-n400", labelKey: "status.cancelled" }
    default:
      return { dot: "bg-n400", labelKey: `status.${status}` }
  }
}

function ReceiptChip({ part }: { part: SkillJobPart }) {
  const { t } = useTranslation("jobs")
  const tone = receiptTone(part.status)
  const name = part.skillKey.replace(/^(builtin|user):/, "")
  return (
    <div className="border-hair bg-n100/50 flex min-w-0 items-center gap-2 rounded-lg border px-3 py-2">
      <span className={cn("size-2 shrink-0 rounded-full", tone.dot)} />
      <span className="text-n800 shrink-0 text-sm font-medium">
        {name} · {part.operation}
      </span>
      <span className="text-n500 shrink-0 text-xs">
        {t(tone.labelKey, { defaultValue: part.status })}
      </span>
      {part.summary ? (
        <span className="text-n600 min-w-0 truncate text-xs">{part.summary}</span>
      ) : null}
    </div>
  )
}

function Receipt({ part }: { part: SkillJobPart }) {
  return (
    <div className="min-w-0">
      <ReceiptChip part={part} />
      {/* The transcript is the only lasting record once the live card rotates
          out, so the produced file belongs here, not just its name. */}
      {(part.artifacts ?? []).map((a) => (
        <AssetPreview key={a.assetId} artifact={a} />
      ))}
    </div>
  )
}

/** Durable transcript record of finished background jobs. The jobs dock shows
 *  live cards; this is what remains after they rotate out. */
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

import { useTranslation } from "react-i18next"
import { formatBytes, formatDateTime } from "@/shared/lib/format"
import { cn } from "@/shared/lib/cn"
import type { ReviewDetail } from "@/features/admin-skills/types"

/** Everything a reviewer checks before opening the archive itself. */
export function ReviewMeta({ detail }: { detail: ReviewDetail }) {
  const { t } = useTranslation("admin-skills")
  const none = t("review.meta.none")
  const rows: { key: string; value: string; mono?: boolean }[] = [
    {
      key: "author",
      value: detail.author ? `${detail.author.username} · ${detail.author.email}` : (detail.publisher ?? "—"),
    },
    { key: "version", value: detail.version == null ? "—" : String(detail.version), mono: true },
    {
      key: "submittedAt",
      value: detail.published_at ? formatDateTime(detail.published_at) : "—",
    },
    { key: "size", value: detail.size ? formatBytes(detail.size) : "—" },
    { key: "requiresMcp", value: detail.requires_mcp?.length ? detail.requires_mcp.join(", ") : none },
    { key: "sha256", value: detail.sha256 || "—", mono: true },
  ]

  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
      {rows.map((row) => (
        <div key={row.key} className="flex min-w-0 gap-2">
          <dt className="text-n600 w-20 flex-none">{t(`review.meta.${row.key}`)}</dt>
          <dd className={cn("min-w-0 break-all", row.mono && "font-mono")}>{row.value}</dd>
        </div>
      ))}
    </dl>
  )
}

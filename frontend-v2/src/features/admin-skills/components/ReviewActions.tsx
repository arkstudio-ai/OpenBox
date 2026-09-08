import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Download } from "lucide-react"
import {
  useApproveSubmission,
  useDownloadArchive,
  useRejectSubmission,
  useSetListing,
} from "@/features/admin-skills/api/admin-skills"
import type { ReviewDetail } from "@/features/admin-skills/types"
import { ListingDialog } from "./ListingDialog"

const BUTTON =
  "rounded-full border border-hair px-3 py-1.5 text-xs text-n800 hover:bg-hairsoft disabled:opacity-50"
const PRIMARY = "rounded-full bg-ink px-3 py-1.5 text-xs text-bg hover:opacity-90 disabled:opacity-50"

/** `approve`/`reject` clear the queue; `list`/`delist` move an already-judged entry. */
type Verdict = "approve" | "reject" | "list" | "delist"

export function ReviewActions({ detail }: { detail: ReviewDetail }) {
  const { t } = useTranslation("admin-skills")
  const [verdict, setVerdict] = useState<Verdict | null>(null)
  const approve = useApproveSubmission()
  const reject = useRejectSubmission()
  const listing = useSetListing()
  const download = useDownloadArchive()

  const awaiting = detail.listing === "pending"
  const busy = approve.isPending || reject.isPending || listing.isPending

  const confirm = (reason: string) => {
    const catalogId = detail.catalog_id
    const settle = { onSuccess: () => setVerdict(null) }
    if (verdict === "approve") approve.mutate({ catalogId, note: reason }, settle)
    else if (verdict === "reject") reject.mutate({ catalogId, note: reason }, settle)
    else if (verdict)
      listing.mutate(
        { catalogId, listing: verdict === "delist" ? "delisted" : "listed", note: reason },
        settle,
      )
  }

  return (
    <div className="border-hair flex flex-wrap items-center justify-end gap-2 border-t pt-3">
      <button
        type="button"
        className={`${BUTTON} me-auto flex items-center gap-1.5`}
        disabled={download.isPending}
        onClick={() => download.mutate({ catalogId: detail.catalog_id, name: detail.name })}
      >
        <Download className="size-3.5" aria-hidden />
        {t("action.download")}
      </button>
      {download.isError && (
        <p role="alert" className="text-danger text-xs">
          {t("review.downloadFailed")}
        </p>
      )}

      {awaiting ? (
        <>
          <button type="button" className={BUTTON} disabled={busy} onClick={() => setVerdict("reject")}>
            {t("action.reject")}
          </button>
          <button type="button" className={PRIMARY} disabled={busy} onClick={() => setVerdict("approve")}>
            {t("action.approve")}
          </button>
        </>
      ) : detail.listing === "listed" ? (
        <button type="button" className={BUTTON} disabled={busy} onClick={() => setVerdict("delist")}>
          {t("action.delist")}
        </button>
      ) : (
        <button type="button" className={PRIMARY} disabled={busy} onClick={() => setVerdict("list")}>
          {t("action.list")}
        </button>
      )}

      <ListingDialog
        open={!!verdict}
        title={t(`dialog.${verdict ?? "approve"}.title`, { title: detail.title })}
        body={t(`dialog.${verdict ?? "approve"}.body`)}
        confirmLabel={t(`action.${verdict ?? "approve"}`)}
        requireReason={verdict === "reject" || verdict === "delist"}
        pending={busy}
        failed={approve.isError || reject.isError || listing.isError}
        onClose={() => setVerdict(null)}
        onConfirm={confirm}
      />
    </div>
  )
}

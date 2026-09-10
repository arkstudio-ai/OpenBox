import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { StatusPill } from "@/shared/ui/StatusPill"
import { DisplayIcon } from "@/shared/ui/DisplayIcon"
import { listingTone } from "@/features/admin-skills/lib/entry"
import { useReviewDetail } from "@/features/admin-skills/api/admin-skills"
import { ReviewActions } from "./ReviewActions"
import { ReviewFiles } from "./ReviewFiles"
import { ReviewMeta } from "./ReviewMeta"

/** One submission, read-only. */
export function ReviewDetail({ catalogId }: { catalogId: string }) {
  const { t } = useTranslation("admin-skills")
  const query = useReviewDetail(catalogId)

  if (query.isPending) {
    return (
      <section className="border-hair bg-card flex flex-1 justify-center rounded-xl border py-16">
        <Spinner className="size-5" />
        <span className="sr-only">{t("list.loading")}</span>
      </section>
    )
  }
  if (query.isError || !query.data) {
    return (
      <section className="border-hair bg-card flex-1 rounded-xl border p-4">
        <p role="alert" className="text-danger py-10 text-center text-sm">
          {t("review.loadFailed")}
        </p>
      </section>
    )
  }

  const detail = query.data
  return (
    <section className="border-hair bg-card flex min-w-0 flex-1 flex-col gap-4 rounded-xl border p-4">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-base font-medium">
            <DisplayIcon icon={detail.icon} />
            {detail.title}
          </h2>
          <p className="text-n600 text-2xs mt-0.5 truncate font-mono">{detail.catalog_id}</p>
        </div>
        <StatusPill tone={listingTone(detail.listing)} title={detail.listing_note ?? undefined}>
          {t(`status.${detail.listing}`)}
        </StatusPill>
      </header>

      {detail.listing_note && <p className="text-n700 text-xs">{detail.listing_note}</p>}

      <ReviewMeta detail={detail} />

      {detail.archive_error ? (
        // The server declined to open this one, so there is no manifest and no
        // file list to show. Falling through to "this archive has no SKILL.md"
        // would state, of bytes nobody read, that they are harmless — and this
        // is the screen where that sentence turns into a 通过 click.
        <p role="alert" className="bg-dangersoft text-danger rounded-lg px-3 py-2 text-xs leading-5">
          {t("review.archiveRefused")}
          <span className="mt-1 block font-mono break-all opacity-80">{detail.archive_error}</span>
        </p>
      ) : (
        <>
          <section className="flex flex-col gap-2">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-n600 text-xs font-medium">{t("review.skillMd")}</h3>
              {/* Submitted by a stranger: it is shown as characters, never parsed
                  as Markdown and never injected as HTML (plan §4.9). */}
              <p className="text-n600 text-2xs">{t("review.skillMdNotice")}</p>
            </div>
            {detail.skill_md ? (
              <pre className="border-hair bg-bg text-n800 text-2xs max-h-96 overflow-auto rounded-lg border p-3 font-mono leading-5 whitespace-pre-wrap">
                {detail.skill_md}
              </pre>
            ) : (
              <p className="text-n600 text-xs">{t("review.skillMdEmpty")}</p>
            )}
            {detail.skill_md_truncated && <p className="text-n600 text-2xs">{t("review.truncated")}</p>}
          </section>

          <ReviewFiles
            files={detail.files ?? []}
            total={detail.files_total}
            truncated={detail.files_truncated}
          />
        </>
      )}

      <ReviewActions detail={detail} />
    </section>
  )
}

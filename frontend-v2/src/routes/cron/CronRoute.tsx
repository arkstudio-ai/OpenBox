import { useTranslation } from "react-i18next"
import { useParams } from "react-router"
import { SessionTranscript } from "@/features/chat"
import { CronJobPage, CronPage } from "@/features/cron"

export default function CronRoute() {
  const { t } = useTranslation("cron")
  const { jobId } = useParams()

  // One task: the page owns the height, so the transcript scrolls inside it
  // like a chat does rather than pushing the run list off the top.
  if (jobId) {
    return (
      <div className="flex min-h-0 flex-1 flex-col px-3 pt-1.5 pb-4 sm:px-6.5">
        <div className="mx-auto flex min-h-0 w-full max-w-[1100px] flex-1 flex-col">
          <CronJobPage
            jobId={jobId}
            renderTranscript={(sessionId) => <SessionTranscript sessionId={sessionId} />}
          />
        </div>
      </div>
    )
  }

  return (
    <div className="scr min-h-0 flex-1 overflow-auto px-6.5 pt-1.5 pb-7">
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-4.5">
        <div className="flex flex-col gap-1">
          <span className="text-2xl font-medium tracking-tight">{t("page.title")}</span>
          <span className="text-n600 text-sm">{t("page.subtitle")}</span>
        </div>
        <CronPage />
      </div>
    </div>
  )
}

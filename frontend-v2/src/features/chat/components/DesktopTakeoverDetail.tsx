// The takeover card: what the agent ran into on a web page (a captcha, a
// risk-control wall, a code only the user can receive) and the one-click way
// to go and deal with it.
//
// It rides on a `question` request whose `detail.kind` is "desktop_takeover"
// (backend/tool/desktop_takeover.py), so the answering pills and the reply
// plumbing are QuestionDock's; this only adds the where and the link. The
// question text already says what to do, so it is not repeated here.
//
// When the page is on the cloud desktop the link opens the desktop panel with
// input control switched on. It is a real URL (paths.desktopTakeover) so it
// survives a reload and can later be handed to a phone; the click handler
// just short-circuits the round trip through the router.
import { useTranslation } from "react-i18next"
import { MonitorUp } from "lucide-react"
import type { QuestionItem } from "@/shared/types/api"
import { emitAppEvent } from "@/shared/events/bus"
import { paths } from "@/shared/router/paths"

export interface TakeoverDetail {
  reason: string
  url: string
  host: string
  page: string
  instructions: string
  /** Where the blocked page is: this desktop's Chrome, or the user's own. */
  browser: "local" | "extension"
}

const REASONS = new Set([
  "captcha_slider",
  "captcha_click",
  "captcha_math",
  "sms_code",
  "login_required",
  "risk_control",
  "other",
])

function text(record: Record<string, unknown>, key: string): string {
  const value = record[key]
  return typeof value === "string" ? value : ""
}

/** The detail of a desktop_takeover question, or null for anything else. */
export function readTakeoverDetail(value: unknown): TakeoverDetail | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  if (record.kind !== "desktop_takeover") return null
  return {
    reason: text(record, "reason") || "other",
    url: text(record, "url"),
    host: text(record, "host"),
    page: text(record, "page"),
    instructions: text(record, "instructions"),
    browser: record.browser === "extension" ? "extension" : "local",
  }
}

/** i18n key for a reason; unknown reasons fall back to the generic label. */
export function takeoverReasonKey(reason: string): string {
  return `takeover.reason.${REASONS.has(reason) ? reason : "other"}`
}

export function DesktopTakeoverDetail({ item, sessionId }: { item: QuestionItem; sessionId: string }) {
  const { t } = useTranslation("chat")
  const detail = readTakeoverDetail(item.detail)
  if (!detail) return null

  const openDesktop = () => emitAppEvent("workbench.open", { kind: "desktop", control: true })

  return (
    <div className="border-hair bg-bg flex flex-col gap-2.5 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-n600">{t("takeover.reasonLabel")}</span>
        <span className="bg-a100 text-a800 rounded-full px-2 py-0.5">{t(takeoverReasonKey(detail.reason))}</span>
        {detail.host && (
          <>
            <span className="text-n600">{t("takeover.site")}</span>
            <span className="bg-hairsoft text-n700 rounded-full px-2 py-0.5" title={detail.url}>
              {detail.host}
            </span>
          </>
        )}
      </div>

      {detail.browser === "local" ? (
        <div className="flex flex-col gap-1.5">
          <a
            href={paths.desktopTakeover(sessionId)}
            onClick={(e) => {
              // Modifier clicks keep their browser meaning (open in a new tab).
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return
              e.preventDefault()
              openDesktop()
            }}
            className="bg-ink text-bg hover:bg-ink/90 inline-flex w-fit items-center gap-1.5 rounded-full px-4 py-1.5 text-sm font-medium underline-offset-2 hover:underline"
          >
            <MonitorUp className="size-4" strokeWidth={2} />
            {t("takeover.open")}
          </a>
          <span className="text-n600 text-xs">{t("takeover.openHint")}</span>
        </div>
      ) : (
        <span className="text-n700 text-sm">{t("takeover.ownBrowser")}</span>
      )}
    </div>
  )
}

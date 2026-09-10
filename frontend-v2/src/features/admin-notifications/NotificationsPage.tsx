import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import { Spinner } from "@/shared/ui/Spinner"
import { sendPushTest, usePushOverview, type SendTest } from "./api"

const button =
  "min-h-10 rounded-full border border-hair px-4 py-2 text-sm hover:bg-hairsoft disabled:opacity-50"
const card = "rounded-xl border border-hair bg-card p-4"

export function NotificationsPage() {
  const { t, i18n } = useTranslation(["admin", "errors"])
  const allowed = useAuthStore((s) => s.user?.role === "admin")
  const locale = i18n.language.startsWith("zh") ? "zh-CN" : "en-US"
  const query = usePushOverview(locale)
  const [template, setTemplate] = useState("system_test")
  const [sending, setSending] = useState(false)
  const [feedback, setFeedback] = useState("")
  const inFlight = useRef(false)
  // Reuse the request ID after a network failure; retry cannot send twice.
  const request = useRef<SendTest | null>(null)
  const data = query.data
  const preview = data?.templates.find((item) => item.id === template)
  const errorText = (error: unknown) =>
    error instanceof ApiError && i18n.exists(error.code, { ns: "errors" })
      ? t(error.code, { ns: "errors" })
      : t("push.requestFailed")
  const send = async () => {
    if (inFlight.current || !data?.device.ready || !data.device.bindingId || !allowed) return
    inFlight.current = true
    setSending(true)
    setFeedback("")
    if (
      !request.current ||
      request.current.bindingId !== data.device.bindingId ||
      request.current.template !== template
    ) {
      request.current = { template, bindingId: data.device.bindingId, requestId: crypto.randomUUID() }
    }
    try {
      await sendPushTest(request.current)
      request.current = null
      setFeedback(t("push.queued"))
      await query.refetch()
    } catch (error) {
      setFeedback(errorText(error))
      if (error instanceof ApiError && error.status < 500) request.current = null
    } finally {
      inFlight.current = false
      setSending(false)
    }
  }
  if (!allowed || (query.error instanceof ApiError && query.error.status === 403))
    return <p>{t("mobile.forbidden")}</p>
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-n600 text-sm">{t("push.scope")}</p>
        <button className={button} onClick={() => void query.refetch()} disabled={query.isFetching}>
          {t("push.refresh")}
        </button>
      </div>
      {query.error && (
        <p role="alert" className="text-danger text-sm">
          {errorText(query.error)}
        </p>
      )}
      {!data && query.isLoading && <Spinner />}
      {data && (
        <>
          <section className={card} aria-label={t("push.device")}>
            <h3 className="mb-3 font-medium">{t("push.device")}</h3>
            <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-n500">{t("push.phone")}</dt>
                <dd>
                  {data.device.registered ? t(`push.platform.${data.device.platform}`) : t("push.noDevice")}
                </dd>
              </div>
              <div>
                <dt className="text-n500">{t("push.permission")}</dt>
                <dd>{t(data.device.notificationsEnabled ? "push.enabled" : "push.disabled")}</dd>
              </div>
              <div>
                <dt className="text-n500">{t("push.appState")}</dt>
                <dd>
                  {t(`push.presence.${data.presence.appState}`, { defaultValue: t("push.presence.unknown") })}
                </dd>
              </div>
              <div>
                <dt className="text-n500">{t("push.channel")}</dt>
                <dd>
                  {data.providers
                    .map(
                      (p) =>
                        `${t(`push.provider.${p.id}`)} · ${t(p.configured ? "push.configured" : "push.unconfigured")}`,
                    )
                    .join(" / ")}
                </dd>
              </div>
            </dl>
            {!data.device.ready && <p className="text-n600 mt-3 text-sm">{t("push.setupHint")}</p>}
          </section>
          <section className={card} aria-label={t("push.preview")}>
            <label className="mb-2 block text-sm font-medium" htmlFor="push-template">
              {t("push.template")}
            </label>
            <select
              id="push-template"
              value={template}
              disabled={sending}
              onChange={(event) => setTemplate(event.target.value)}
              className="border-hair bg-card min-h-10 w-full rounded-lg border px-3 text-sm"
            >
              {data.templates.map((item) => (
                <option key={item.id} value={item.id}>
                  {t(`push.templateName.${item.id}`)}
                </option>
              ))}
            </select>
            {preview && (
              <div className="bg-hairsoft my-4 rounded-xl px-4 py-3" aria-label={t("push.preview")}>
                <p className="text-sm font-medium">{preview.title}</p>
                <p className="text-n600 mt-1 text-sm break-words">{preview.body}</p>
              </div>
            )}
            <p className="text-n600 mb-3 text-sm">{t("push.instructions")}</p>
            <button className={button} onClick={() => void send()} disabled={sending || !data.device.ready}>
              {t(sending ? "push.sending" : "push.send")}
            </button>
            {feedback && (
              <p role="status" className="mt-3 text-sm">
                {feedback}
              </p>
            )}
          </section>
          <section className={card} aria-label={t("push.history")}>
            <h3 className="font-medium">{t("push.history")}</h3>
            <p className="text-n600 my-2 text-sm">{t("push.receiptHint")}</p>
            {data.tests.length === 0 ? (
              <p className="text-n500 text-sm">{t("push.empty")}</p>
            ) : (
              <ul className="divide-hair divide-y">
                {data.tests.map((item) => (
                  <li key={item.id} className="py-3 text-sm">
                    <div className="flex flex-wrap justify-between gap-2">
                      <span className="font-medium">{item.title}</span>
                      <span>{t(`push.status.${item.receipt ?? item.status}`)}</span>
                    </div>
                    <p className="text-n600 mt-1 break-words">{item.body}</p>
                    {item.error && (
                      <p className="text-n600 mt-1">
                        {t(`push.reason.${item.error}`, {
                          defaultValue: t(
                            item.error.startsWith("presence_")
                              ? "push.waitingBackground"
                              : "push.deliveryFailed",
                          ),
                        })}
                      </p>
                    )}
                    <time className="text-n500 mt-1 block text-xs" dateTime={item.createdAt}>
                      {new Date(item.createdAt).toLocaleString(locale)}
                    </time>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  )
}

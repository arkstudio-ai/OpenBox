import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check } from "lucide-react"
import { useCreatePaymentOrder, usePaymentProviders } from "@/shared/api/billing"
import type { BillingCycle, BillingPlans, BillingSubscription } from "@/shared/api/billing"
import { formatCredits, formatDateTime, formatNumber } from "@/shared/lib/format"
import { cn } from "@/shared/lib/cn"
import { useCheckoutRedirect } from "../hooks/useCheckoutRedirect"
import { PaymentChannelPicker } from "./PaymentChannelPicker"

const CYCLES = ["monthly", "yearly"] as const

export function PlanOptions({
  catalog,
  subscription,
}: {
  catalog: BillingPlans
  subscription: BillingSubscription
}) {
  const { t } = useTranslation("billing")
  const [cycle, setCycle] = useState<BillingCycle>("monthly")
  const [selectedProvider, setSelectedProvider] = useState("")
  const providers = usePaymentProviders()
  const create = useCreatePaymentOrder()
  const redirect = useCheckoutRedirect()
  const attempt = useRef<{ params: string; key: string } | null>(null)
  const channels = providers.data?.items ?? []
  const provider =
    channels.find((item) => item.id === selectedProvider)?.id ??
    channels.find((item) => item.id === "alipay")?.id ??
    channels[0]?.id ??
    ""
  const scheduledStart = subscription.queued.at(-1)?.ends_at ?? subscription.ends_at
  return (
    <section className="flex min-w-0 flex-col gap-4" aria-label={t("tabs.purchase")}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-medium">{t("plans.title")}</h1>
        {channels.length > 0 && (
          <PaymentChannelPicker providers={channels} value={provider} onChange={setSelectedProvider} />
        )}
        <div className="bg-n200 flex rounded-full p-1" role="group" aria-label={t("plans.cycleLabel")}>
          {CYCLES.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={cycle === value}
              onClick={() => setCycle(value)}
              className={cn(
                "rounded-full px-4 py-1.5 text-xs",
                cycle === value ? "bg-card text-ink shadow-sm" : "text-n600",
              )}
            >
              {t(`plans.cycles.${value}`)}
            </button>
          ))}
        </div>
      </div>
      <div className="grid min-w-0 grid-cols-1 gap-3 @min-[680px]/billing:grid-cols-3">
        {catalog.plans.map((plan) => (
          <article
            key={plan.id}
            aria-label={t(`plans.names.${plan.id}`)}
            className={cn(
              "bg-card flex min-w-0 flex-col gap-4 rounded-xl border p-5",
              plan.recommended ? "border-ink" : "border-hair",
            )}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-base font-medium">{t(`plans.names.${plan.id}`)}</h2>
              {plan.id === subscription.plan_id ? (
                <span className="bg-n200 text-2xs rounded-full px-2 py-1">{t("plans.current")}</span>
              ) : plan.recommended ? (
                <span className="bg-ink text-bg text-2xs rounded-full px-2 py-1">
                  {t("plans.recommended")}
                </span>
              ) : null}
            </div>
            <div className="flex flex-wrap items-baseline gap-1">
              <span className="text-3xl font-medium tabular-nums">
                ¥{formatNumber(plan.prices_fen[cycle] / 100)}
              </span>
              <span className="text-n600 text-xs">{t(`plans.per.${cycle}`)}</span>
            </div>
            <p className="text-sm font-medium">
              {t(`plans.allowance.${plan.credit_period}`, { credits: formatCredits(plan.credits) })}
            </p>
            <ul className="text-n700 flex flex-1 flex-col gap-2 text-xs">
              <li className="flex items-start gap-2">
                <Check className="size-3.5 flex-none" />
                {t("plans.modelUsage")}
              </li>
              <li className="flex items-start gap-2">
                <Check className="size-3.5 flex-none" />
                {t(plan.id === "free" ? "plans.weeklyGrant" : "plans.monthlyGrant")}
              </li>
              {plan.id !== "free" && (
                <li className="flex items-start gap-2">
                  <Check className="size-3.5 flex-none" />
                  {t("plans.topupAvailable")}
                </li>
              )}
            </ul>
            <button
              type="button"
              disabled={plan.id === "free" || !provider || !subscription.can_manage || create.isPending}
              className={cn(
                "rounded-full px-4 py-2.5 text-sm disabled:opacity-50",
                plan.recommended ? "bg-ink text-bg" : "bg-n200 text-ink",
              )}
              onClick={() => {
                if (plan.id === "free") return
                const params = `${provider}:${plan.id}:${cycle}`
                if (attempt.current?.params !== params || create.data?.status === "paid")
                  attempt.current = { params, key: crypto.randomUUID() }
                const planId = plan.id
                const requestKey = attempt.current.key
                void redirect(() =>
                  create.mutateAsync({
                    kind: "subscription",
                    plan_id: planId,
                    cycle,
                    provider,
                    request_key: requestKey,
                  }),
                )
              }}
            >
              {plan.id === "free"
                ? t(subscription.plan_id === "free" ? "plans.current" : "plans.freeFallback")
                : create.isPending &&
                    create.variables?.kind === "subscription" &&
                    create.variables.plan_id === plan.id
                  ? t("usage.creatingOrder")
                  : !subscription.can_manage
                    ? t("plans.managerOnly")
                    : !provider
                      ? t("usage.paymentUnavailable")
                      : t(scheduledStart ? "plans.renew" : "plans.subscribe")}
            </button>
          </article>
        ))}
      </div>
      {providers.data?.reason === "PAYMENT_REGION_UNAVAILABLE" && (
        <p role="note" className="text-n700 text-xs">
          {t("usage.paymentRegionNote")}
        </p>
      )}
      {providers.isError && (
        <p role="alert" className="text-n600 text-xs">
          {t("usage.providerError")}
        </p>
      )}
      {create.isError && (
        <p role="alert" className="text-n600 text-xs">
          {t("usage.paymentError")}
        </p>
      )}
      {scheduledStart && (
        <p className="text-n600 text-xs">
          {t("plans.renewStarts", { date: formatDateTime(scheduledStart) })}
        </p>
      )}
    </section>
  )
}

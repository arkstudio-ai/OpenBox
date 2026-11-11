import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { useCreatePaymentOrder, usePaymentProviders } from "@/shared/api/billing"
import { formatCredits } from "@/shared/lib/format"
import type { TopupRules } from "@/shared/api/billing"
import { useCheckoutRedirect } from "../hooks/useCheckoutRedirect"

export function CreditTopUp({
  rules,
  allowed,
  canManage,
}: {
  rules: TopupRules
  allowed: boolean
  canManage: boolean
}) {
  const { t } = useTranslation("billing")
  const providers = usePaymentProviders()
  const create = useCreatePaymentOrder()
  const [amount, setAmount] = useState("10")
  const [selected, setSelected] = useState("")
  const attempt = useRef<{ params: string; key: string } | null>(null)
  const redirect = useCheckoutRedirect()
  const provider =
    selected ||
    providers.data?.items.find((item) => item.id === "alipay")?.id ||
    providers.data?.items[0]?.id ||
    ""
  const amountFen = /^\d+(\.\d{1,2})?$/.test(amount) ? Math.round(Number(amount) * 100) : 0
  const valid = amountFen >= rules.min_amount_fen && amountFen <= rules.max_amount_fen
  const paid = create.data?.status === "paid"

  if (!allowed) return <p className="text-n600 text-sm">{t("plans.topupRequiresPaid")}</p>
  if (!canManage) return <p className="text-n600 text-sm">{t("plans.managerOnly")}</p>
  if (providers.isLoading) return null
  if (providers.isError) return <p className="text-n600 text-xs">{t("usage.providerError")}</p>
  if (providers.data?.reason === "PAYMENT_REGION_UNAVAILABLE")
    return <p className="text-n600 text-sm">{t("usage.paymentRegionNote")}</p>
  if (!providers.data?.items.length)
    return (
      <button type="button" disabled className="bg-n200 text-n600 self-start rounded-full px-4 py-2 text-sm">
        {t("usage.paymentUnavailable")}
      </button>
    )

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (!valid || create.isPending) return
        const params = `${provider}:${amountFen}`
        if (attempt.current?.params !== params || paid) attempt.current = { params, key: crypto.randomUUID() }
        const requestKey = attempt.current.key
        void redirect(() => create.mutateAsync({ provider, amount_fen: amountFen, request_key: requestKey }))
      }}
    >
      <div className="flex flex-wrap gap-2">
        {rules.presets_fen.map((value) => (
          <button
            type="button"
            key={value}
            aria-pressed={amountFen === value}
            onClick={() => setAmount(String(value / 100))}
            className="border-hair aria-pressed:bg-n200 rounded-full border px-4 py-2 text-xs"
          >
            {t("usage.points", { value: formatCredits(String(value / 100)) })}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-n600 flex min-w-0 flex-1 flex-col gap-1 text-xs">
          {t("usage.topUpPoints")}
          <input
            className="border-hair bg-bg text-ink w-full rounded-lg border px-3 py-2 text-sm"
            type="number"
            min={rules.min_amount_fen / 100}
            max={rules.max_amount_fen / 100}
            step={0.01}
            required
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
          />
        </label>
        <label className="text-n600 flex min-w-0 flex-1 flex-col gap-1 text-xs">
          {t("usage.paymentChannel")}
          <select
            className="border-hair bg-bg text-ink w-full rounded-lg border px-3 py-2 text-sm"
            value={provider}
            onChange={(event) => setSelected(event.target.value)}
          >
            {providers.data.items.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          disabled={!valid || create.isPending}
          className="bg-ink text-bg rounded-full px-4 py-2 text-sm disabled:opacity-40"
        >
          {create.isPending ? t("usage.creatingOrder") : t("usage.topUp")}
        </button>
      </div>
      {create.isError && (
        <p role="alert" className="text-n600 text-xs">
          {t("usage.paymentError")}
        </p>
      )}
    </form>
  )
}

// State and actions behind the edit sheet: the draft fragments, the request
// key (one per attempt, renewed after success), and the three writes.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "@/shared/ui/Toast"
import { usePricingPreview, useRevertPricing, useWritePricing } from "../api"
import { fieldValues, fragmentValid, isDecimal, newRequestKey, SAMPLE_USAGE, toFragment } from "../lib"
import type { PricingItem, PricingWriteBody } from "../types"

export function usePricingEditor(item: PricingItem) {
  const { t } = useTranslation("admin-pricing")
  const write = useWritePricing()
  const revert = useRevertPricing()
  const preview = usePricingPreview()
  const [sale, setSale] = useState(() => fieldValues(item.kind, item.sale))
  const [cost, setCost] = useState(() => fieldValues(item.kind, item.cost))
  const [editCost, setEditCost] = useState(false)
  const [basis, setBasis] = useState(item.cost?.basis ?? "")
  const [source, setSource] = useState(item.cost?.source ?? "")
  const [reason, setReason] = useState("")
  const [validUntil, setValidUntil] = useState("")
  const [allowBelowCost, setAllowBelowCost] = useState(false)
  const [confirmDisable, setConfirmDisable] = useState(false)
  const [requestKey, setRequestKey] = useState(newRequestKey)
  const [error, setError] = useState<unknown>(null)

  const busy = write.isPending || revert.isPending
  const revision = item.rule?.revision ?? 0
  const saleValid = fragmentValid(item.kind, sale)
  const costValid = !editCost || Object.values(cost).every((v) => v.trim() === "" || isDecimal(v))
  const hasReason = reason.trim() !== ""
  const canSave = saleValid && costValid && hasReason && !busy
  const canDisable = hasReason && confirmDisable && !busy && item.rule?.status !== "disabled"

  const costFragment = () =>
    editCost
      ? {
          ...toFragment(item.kind, cost),
          currency: "CNY",
          basis: basis.trim() || undefined,
          source: source.trim() || undefined,
          verified_at: new Date().toISOString().slice(0, 10),
        }
      : null

  const body = (status: "active" | "disabled"): PricingWriteBody => ({
    request_key: requestKey,
    reason: reason.trim(),
    expected_revision: revision,
    status,
    sale: status === "active" ? toFragment(item.kind, sale) : null,
    cost: costFragment(),
    valid_until: validUntil ? new Date(validUntil).toISOString() : null,
    allow_below_cost: allowBelowCost,
    confirm_disable: status === "disabled" && confirmDisable,
  })

  const finish = (message: string) => {
    toast.success(message)
    setRequestKey(newRequestKey())
    setReason("")
    setError(null)
  }

  const save = (status: "active" | "disabled") => {
    setError(null)
    write.mutate(
      { key: item.key, body: body(status) },
      {
        onSuccess: (result) => finish(t("editor.saved", { seconds: result.effective_within_seconds })),
        onError: setError,
      },
    )
  }

  const doRevert = () => {
    if (!item.rule || !window.confirm(t("editor.revertConfirm"))) return
    setError(null)
    revert.mutate(
      {
        key: item.key,
        body: { request_key: requestKey, reason: reason.trim() || t("editor.revertReason"), expected_revision: revision },
      },
      { onSuccess: () => finish(t("editor.reverted")), onError: setError },
    )
  }

  const runPreview = () =>
    preview.mutate({
      key: item.key,
      usage: SAMPLE_USAGE[item.kind],
      sale: saleValid ? toFragment(item.kind, sale) : undefined,
    })

  return {
    sale,
    setSale,
    cost,
    setCost,
    editCost,
    setEditCost,
    basis,
    setBasis,
    source,
    setSource,
    reason,
    setReason,
    validUntil,
    setValidUntil,
    allowBelowCost,
    setAllowBelowCost,
    confirmDisable,
    setConfirmDisable,
    error,
    busy,
    revision,
    canSave,
    canDisable,
    save,
    doRevert,
    preview,
    runPreview,
  }
}

export type PricingEditor = ReturnType<typeof usePricingEditor>

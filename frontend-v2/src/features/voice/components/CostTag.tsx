import type { TFunction } from "i18next"
import { useTranslation } from "react-i18next"
import { formatAmount } from "@/shared/lib/format"
import { COST_ITEMS, type CostItem, type CostSnapshot } from "../lib/types"

const ITEM_COPY: Record<CostItem, string> = {
  input_text: "cost.items.inputText",
  input_audio: "cost.items.inputAudio",
  output_text: "cost.items.outputText",
  output_audio: "cost.items.outputAudio",
}

/** Calls are paid in credits (1 credit = 1 yuan): the meter's yuan are shown as credits. */
function credits(t: TFunction<"voice">, yuan: string, digits: number): string {
  return t("cost.credits", { amount: formatAmount(yuan, digits) })
}

/** The hover breakdown: the four meter items, the settled rounds, and any caveat. */
function breakdown(t: TFunction<"voice">, cost: CostSnapshot): string {
  const lines = [`${t("controls.cost")} ${credits(t, cost.total_yuan, 4)}`]
  for (const item of COST_ITEMS) {
    const amount = cost.costs_yuan?.[item]
    if (amount !== undefined) lines.push(`${t(ITEM_COPY[item])} ${credits(t, amount, 6)}`)
  }
  lines.push(t("cost.settled", { count: cost.settled_rounds ?? 0 }))
  if ((cost.unreported_rounds ?? 0) > 0) lines.push(t("cost.partial"))
  else if (cost.pending) lines.push(t("cost.pending"))
  return lines.join("\n")
}

/** The running estimate in the control row; the server does the arithmetic. */
export function CostTag({ cost }: { cost: CostSnapshot }) {
  const { t } = useTranslation("voice")
  return (
    <span title={breakdown(t, cost)} className="text-n600 flex-none text-xs tabular-nums">
      {credits(t, cost.total_yuan, 4)}
    </span>
  )
}

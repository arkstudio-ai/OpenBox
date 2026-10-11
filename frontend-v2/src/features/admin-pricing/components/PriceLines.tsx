// One line per price field: "输入 6" / "输出 30". The table shows cost and
// sale side by side in this shape so the eye compares like with like.
import { useTranslation } from "react-i18next"
import { formatCredits } from "@/shared/lib/format"
import { cn } from "@/shared/lib/cn"
import { DASH, FIELDS } from "../lib"
import type { PricingKind } from "../types"

export function PriceLines({
  kind,
  values,
  unit,
  highlight = [],
  muted,
}: {
  kind: PricingKind
  /** field → credits per unit, already in CNY. */
  values: Record<string, string>
  unit: string
  /** Fields drawn in the danger colour (below cost). */
  highlight?: readonly string[]
  muted?: boolean
}) {
  const { t } = useTranslation("admin-pricing")
  const fields = FIELDS[kind].filter((field) => values[field] !== undefined)
  if (fields.length === 0) return <span className="text-n500">{DASH}</span>
  const single = fields.length === 1 && kind !== "llm" && kind !== "voice-realtime"
  return (
    <ul className={cn("space-y-0.5 text-xs leading-4", muted && "text-n500")}>
      {fields.map((field) => (
        <li key={field} className={cn("flex gap-1.5 whitespace-nowrap", highlight.includes(field) && "text-dangerink")}>
          {!single && <span className="text-n500 w-14 flex-none">{t(`fields.${field}`)}</span>}
          <span className="font-mono tabular-nums">{formatCredits(values[field])}</span>
          <span className="text-n500">{t(`units.${unit}`)}</span>
        </li>
      ))}
    </ul>
  )
}

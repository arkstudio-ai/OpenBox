import { useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Check } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { Menu } from "@/shared/ui/Menu"
import type { ModelTier } from "@/shared/types/api"

export interface TierOption {
  tier: ModelTier
  /** What the pill and the row say: "deep", "fast", "high". */
  label: string
  /** What the tier resolves to, in small print, so the choice is never
   *  opaque about what it costs. */
  hint: string
}

interface Props {
  icon: ReactNode
  title: string
  options: TierOption[]
  activeTier?: ModelTier
  /** The pill's text when the current choice matches no tier — a session
   *  pinned to a retired model, or a pick made from the catalogue below. It
   *  names the real thing rather than saying "custom". */
  fallbackLabel: string
  onPick: (tier: ModelTier) => void
  /** The catalogue behind the tiers, for those allowed to see it. Rendered
   *  inside the same menu so an advanced pick still closes it. */
  catalogue?: (close: () => void) => ReactNode
  className?: string
}

/** A composer pill offering three tiers instead of a model catalogue.
 *
 *  A catalogue asks the person to know what each model is. A tier asks only
 *  how much they want to spend on this conversation — the deployment decides
 *  what "deep" resolves to. The row still shows the resolved name in small
 *  print, because a price signal with nothing behind it is not one.
 */
export function TierPicker({
  icon,
  title,
  options,
  activeTier,
  fallbackLabel,
  onPick,
  catalogue,
  className,
}: Props) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)

  const close = () => {
    setOpen(false)
    setExpanded(false)
  }
  const active = options.find((option) => option.tier === activeTier)

  return (
    <div className={cn("relative", className)}>
      <Menu open={open} onClose={close} className={cn("end-0 bottom-10", expanded ? "w-80" : "w-64")}>
        {options.map((option) => {
          const selected = option.tier === activeTier
          return (
            <button
              key={option.tier}
              type="button"
              role="menuitemradio"
              aria-checked={selected}
              onClick={() => {
                onPick(option.tier)
                close()
              }}
              className="hover:bg-hairsoft flex items-center gap-2.5 rounded-full px-3 py-2 text-start"
            >
              <Check
                className={cn("size-3.5 flex-none", selected ? "text-ink" : "opacity-0")}
                strokeWidth={2.4}
              />
              <span className="text-ink flex-none text-sm font-medium">{option.label}</span>
              <span className="text-n600 text-2xs min-w-0 flex-1 truncate text-end">{option.hint}</span>
            </button>
          )
        })}
        {catalogue && (
          <>
            <div className="border-hair my-1 border-t" />
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="text-n600 hover:bg-hairsoft hover:text-ink rounded-full px-3 py-1.5 text-start text-xs"
            >
              {expanded ? t("tier.less") : t("tier.more")}
            </button>
            {expanded && catalogue(close)}
          </>
        )}
      </Menu>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        title={title}
        className="hover:bg-hairsoft flex h-8 items-center gap-2 rounded-full px-3"
      >
        {icon}
        <span className="text-ink text-sm font-medium">{active?.label ?? fallbackLabel}</span>
        <span className="text-n600 text-2xs">▾</span>
      </button>
    </div>
  )
}

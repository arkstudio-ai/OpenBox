import { useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Check } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { Menu } from "@/shared/ui/Menu"
import type { ModelTier } from "@/shared/types/api"

/** A choice inside a tier — for video, a resolution with its price. */
export interface TierChip {
  id: string
  label: string
  /** Small print beside the label: the per-second price, when known. */
  note?: string
}

export interface TierOption {
  tier: ModelTier
  /** What the pill and the row say: "deep", "fast", "高清". */
  label: string
  /** What the tier resolves to, in small print, so the choice is never
   *  opaque about what it costs. */
  hint: string
  /** One line under the label: what the tier is for. */
  description?: string
  /** Choices inside the tier. One chip renders as a plain row. */
  chips?: TierChip[]
}

interface Props {
  icon: ReactNode
  title: string
  options: TierOption[]
  activeTier?: ModelTier
  /** The chip picked inside the active tier, if the tier has chips. */
  activeChip?: string
  /** The pill's text when the current choice matches no tier — a session
   *  pinned to a retired model, or a pick made from the catalogue below. It
   *  names the real thing rather than saying "custom". */
  fallbackLabel: string
  onPick: (tier: ModelTier, chip?: string) => void
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
 *
 *  A tier may hold choices of its own (a video tier's resolutions). They sit
 *  inline as chips with their price, so one click picks the pair and the
 *  cost of stepping up is visible before it happens.
 */
export function TierPicker({
  icon,
  title,
  options,
  activeTier,
  activeChip,
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
  const pillChip = active?.chips?.find((chip) => chip.id === activeChip)
  const withChips = options.some((option) => (option.chips?.length ?? 0) > 1)

  return (
    <div className={cn("relative", className)}>
      <Menu
        open={open}
        onClose={close}
        className={cn("end-0 bottom-10", expanded || withChips ? "w-96" : "w-64")}
      >
        {options.map((option) => {
          const selected = option.tier === activeTier
          const chips = option.chips ?? []
          const pickTier = () => {
            if (chips.length === 0) onPick(option.tier)
            // Re-picking the active tier keeps the chip already on it.
            else onPick(option.tier, selected && activeChip ? activeChip : chips[0].id)
            close()
          }
          return (
            <div
              key={option.tier}
              className={cn("flex flex-col rounded-2xl px-3 py-2", selected && "bg-hairsoft")}
            >
              <button
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                onClick={pickTier}
                className="flex items-center gap-2.5 text-start"
              >
                <Check
                  className={cn("size-3.5 flex-none", selected ? "text-ink" : "opacity-0")}
                  strokeWidth={2.4}
                />
                <span className="text-ink flex-none text-sm font-medium">{option.label}</span>
                <span className="text-n600 text-2xs min-w-0 flex-1 truncate text-end">{option.hint}</span>
              </button>
              {option.description && <span className="text-n600 ps-6 text-xs">{option.description}</span>}
              {chips.length > 1 && (
                <span className="flex flex-wrap items-center gap-1 ps-6 pt-1.5">
                  {chips.map((chip) => {
                    const picked = selected && chip.id === activeChip
                    return (
                      <button
                        key={chip.id}
                        type="button"
                        onClick={() => {
                          onPick(option.tier, chip.id)
                          close()
                        }}
                        className={cn(
                          "text-2xs flex items-baseline gap-1 rounded-full px-2 py-1 tabular-nums",
                          picked ? "bg-ink text-paper" : "text-n600 hover:bg-hairline hover:text-ink",
                        )}
                      >
                        <span className="font-medium">{chip.label}</span>
                        {chip.note && (
                          <span className={cn(picked ? "text-paper/80" : "text-n500")}>{chip.note}</span>
                        )}
                      </button>
                    )
                  })}
                </span>
              )}
              {chips.length === 1 && chips[0].note && (
                <span className="text-n500 text-2xs ps-6 pt-0.5 tabular-nums">
                  {chips[0].label} {chips[0].note}
                </span>
              )}
            </div>
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
        {pillChip && <span className="text-n600 text-2xs tabular-nums">{pillChip.label}</span>}
        <span className="text-n600 text-2xs">▾</span>
      </button>
    </div>
  )
}

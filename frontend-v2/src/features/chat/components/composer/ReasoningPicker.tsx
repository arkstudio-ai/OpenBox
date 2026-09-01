import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Brain, Check, ChevronDown } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { Menu } from "@/shared/ui/Menu"

interface Props {
  variants: string[]
  activeId?: string | null
  defaultId?: string | null
  onPick: (id: string | null) => void
}

/** A model variant is its reasoning strength in the OpenCode/LiteLLM
 * contract. It stays separate from model selection because each model exposes
 * a different ordered set, and models without variants should show nothing. */
export function ReasoningPicker({ variants, activeId, defaultId, onPick }: Props) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)

  if (variants.length === 0) return null

  const levelLabel = (id: string) => {
    const known = ["off", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
    return known.includes(id) ? t(`reasoning.level.${id}`) : id
  }
  const activeLabel = activeId ? levelLabel(activeId) : t("reasoning.default")
  const defaultLabel = defaultId
    ? t("reasoning.defaultWithLevel", { level: levelLabel(defaultId) })
    : t("reasoning.default")

  const option = (id: string | null, label: string) => {
    const selected = id === activeId || (id === null && !activeId)
    return (
      <button
        key={id ?? "default"}
        type="button"
        role="menuitemradio"
        aria-checked={selected}
        onClick={() => {
          onPick(id)
          setOpen(false)
        }}
        className="hover:bg-hairsoft flex items-center gap-2.5 rounded-full px-3 py-2 text-start"
      >
        <Check className={cn("size-3.5 flex-none", selected ? "text-ink" : "opacity-0")} strokeWidth={2.4} />
        <span className="text-ink min-w-0 flex-1 truncate text-sm">{label}</span>
      </button>
    )
  }

  return (
    <div className="relative flex-none">
      <Menu open={open} onClose={() => setOpen(false)} className="end-0 bottom-10 w-48">
        {option(null, defaultLabel)}
        {variants.map((id) => option(id, levelLabel(id)))}
      </Menu>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        title={t("reasoning.pick")}
        aria-label={t("reasoning.current", { level: activeLabel })}
        className="text-n600 hover:bg-hairsoft hover:text-ink flex h-8 items-center gap-1.5 rounded-full px-2.5 text-sm transition-colors"
      >
        <Brain className="size-4 flex-none" />
        <span>{activeLabel}</span>
        <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
      </button>
    </div>
  )
}

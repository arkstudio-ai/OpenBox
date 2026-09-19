import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Check } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { Menu } from "@/shared/ui/Menu"
import type { ModelInfo } from "@/shared/types/api"
import { modelLabel } from "../../lib/model"
import { ModelLogo } from "../ModelLogo"

interface Props {
  models: ModelInfo[]
  activeId?: string
  onPick: (id: string) => void
}

/** The catalogue rows, one per model. Shared with the tier picker, where
 *  they sit under the tiers as the admin's escape hatch. */
export function ModelMenuRows({ models, activeId, onPick }: Props) {
  return (
    <>
      {models.map((m) => (
        <button
          key={m.id}
          type="button"
          onClick={() => onPick(m.id)}
          className="hover:bg-hairsoft flex items-center gap-2.5 rounded-full px-3 py-2 text-start"
        >
          <ModelLogo
            id={m.id}
            className={cn("size-4 flex-none", m.id === activeId ? "text-ink" : "text-n600")}
          />
          <span className="text-ink min-w-0 flex-1 truncate text-sm">{m.name}</span>
          {/* A check, not the provider field: behind an OpenAI-compatible
              gateway every model reports "openai", so that column said the
              same wrong thing on every row. */}
          {m.id === activeId && <Check className="text-ink size-3.5 flex-none" strokeWidth={2.4} />}
        </button>
      ))}
    </>
  )
}

/** The composer's model pill: `⊙ name ▾` opening a checked list. */
export function ModelPicker({ models, activeId, onPick }: Props) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  // A session can be pinned to a model no longer in the list — one retired
  // since, or served by a provider that has been swapped out. It still has to
  // read as a name rather than a routing id.
  const activeName = activeId ? modelLabel(activeId, models) : ""

  return (
    <div className="relative ms-auto max-w-full min-w-0">
      <Menu open={open} onClose={() => setOpen(false)} className="end-0 bottom-10 w-60">
        <ModelMenuRows
          models={models}
          activeId={activeId}
          onPick={(id) => {
            onPick(id)
            setOpen(false)
          }}
        />
      </Menu>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={t("model.pick")}
        className="hover:bg-hairsoft flex h-8 max-w-full items-center gap-2 rounded-full px-3"
      >
        <ModelLogo id={activeId ?? ""} className="text-ink size-4 flex-none" />
        <span className="text-ink truncate text-sm font-medium">{activeName}</span>
        <span className="text-n600 text-2xs">▾</span>
      </button>
    </div>
  )
}

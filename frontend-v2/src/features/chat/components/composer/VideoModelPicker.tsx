import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Clapperboard } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { Menu } from "@/shared/ui/Menu"
import type { VideoModelInfo } from "@/shared/types/api"

interface Props {
  models: VideoModelInfo[]
  activeId?: string
  activeResolution?: string
  onPick: (id: string, resolution: string) => void
}

/** The composer's video-model pill, beside the chat-model one.
 *
 *  A single shared icon rather than per-vendor logos: these are all served
 *  through the same gateway, and the row that matters to the reader is the
 *  price tier, not the badge.
 *
 *  Resolution sits inline on each row rather than behind a hover submenu. A
 *  submenu had to float outside the menu box to avoid clipping, which put it
 *  over the conversation with nothing tying it to its row; it also left the
 *  single-tier models with no chevron and no tier, reading as if they could
 *  not be chosen at all. Inline chips show every model what it offers, and
 *  one click picks the pair.
 */
export function VideoModelPicker({ models, activeId, activeResolution, onPick }: Props) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)

  // A conversation can be pinned to a model no longer offered — retired, or
  // dropped from allowed_models since. Show the id rather than nothing.
  const active = models.find((m) => m.id === activeId)
  const activeName = active?.name ?? activeId ?? ""

  if (models.length === 0) return null

  const choose = (model: VideoModelInfo, resolution: string) => {
    onPick(model.id, resolution)
    setOpen(false)
  }

  return (
    <div className="relative">
      <Menu open={open} onClose={() => setOpen(false)} className="end-0 bottom-10 w-80">
        {models.map((m) => {
          const tiers = m.resolutions ?? []
          const isActive = m.id === activeId
          return (
            <div
              key={m.id}
              className={cn(
                "flex items-center gap-2 rounded-full py-1.5 pe-1.5 ps-3",
                isActive && "bg-hairsoft",
              )}
            >
              <Clapperboard
                className={cn("size-4 flex-none", isActive ? "text-ink" : "text-n600")}
              />
              <button
                type="button"
                onClick={() => choose(m, activeOrFirst(tiers, isActive, activeResolution))}
                className="text-ink min-w-0 flex-1 truncate text-start text-sm"
              >
                {m.name}
              </button>
              {/* The tier, not the channel: what a switch costs is the only
                  thing a reader can act on — the wire channel is our problem. */}
              {m.tier && <span className="text-n600 text-2xs flex-none">{m.tier}</span>}
              <span className="flex flex-none items-center gap-0.5">
                {tiers.map((tier) => {
                  const picked = isActive && tier === activeResolution
                  return (
                    <button
                      key={tier}
                      type="button"
                      onClick={() => choose(m, tier)}
                      className={cn(
                        "text-2xs rounded-full px-1.5 py-1 tabular-nums",
                        picked
                          ? "bg-ink text-paper"
                          : "text-n600 hover:bg-hairline hover:text-ink",
                      )}
                    >
                      {tier}
                    </button>
                  )
                })}
              </span>
            </div>
          )
        })}
      </Menu>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={t("videoModel.pick")}
        className="hover:bg-hairsoft flex h-8 items-center gap-2 rounded-full px-3"
      >
        <Clapperboard className="text-ink size-4" />
        <span className="text-ink text-sm font-medium">{activeName}</span>
        {activeResolution && (
          <span className="text-n600 text-2xs tabular-nums">{activeResolution}</span>
        )}
        <span className="text-n600 text-2xs">▾</span>
      </button>
    </div>
  )
}

/** Clicking the name keeps the tier you were already on, where it exists. */
function activeOrFirst(tiers: string[], isActive: boolean, current?: string): string {
  if (isActive && current && tiers.includes(current)) return current
  return tiers[0] ?? ""
}

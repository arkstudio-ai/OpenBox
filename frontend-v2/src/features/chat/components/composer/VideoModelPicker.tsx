import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, ChevronRight, Clapperboard } from "lucide-react"
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
 *  Resolution hangs off each model as a submenu rather than sitting in its own
 *  pill, because the pair is what gets generated: one model offers 480p and
 *  another only 1080p, so a standalone resolution control would spend most of
 *  its life offering choices the current model cannot honour.
 */
export function VideoModelPicker({ models, activeId, activeResolution, onPick }: Props) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const closeTimer = useRef<number | undefined>(undefined)

  // A conversation can be pinned to a model no longer offered — retired, or
  // dropped from allowed_models since. Show the id rather than nothing.
  const active = models.find((m) => m.id === activeId)
  const activeName = active?.name ?? activeId ?? ""

  useEffect(() => () => window.clearTimeout(closeTimer.current), [])

  if (models.length === 0) return null

  // Collapse the submenu here rather than in an effect watching `open`:
  // setState inside an effect cascades a second render for no benefit.
  const close = () => {
    setOpen(false)
    setExpanded(null)
  }

  const choose = (model: VideoModelInfo, resolution: string) => {
    onPick(model.id, resolution)
    close()
  }

  const resolutionsOf = (model: VideoModelInfo) => model.resolutions ?? []

  return (
    <div className="relative">
      <Menu open={open} onClose={close} className="end-0 bottom-10 w-72">
        {models.map((m) => {
          const tiers = resolutionsOf(m)
          const isActive = m.id === activeId
          // One tier is not a choice — picking the model already made it.
          const hasSubmenu = tiers.length > 1
          return (
            <div
              key={m.id}
              className="relative"
              onMouseEnter={() => {
                window.clearTimeout(closeTimer.current)
                setExpanded(m.id)
              }}
              onMouseLeave={() => {
                closeTimer.current = window.setTimeout(() => setExpanded(null), 160)
              }}
            >
              <button
                type="button"
                onClick={() => choose(m, tiers[0] ?? "")}
                className="hover:bg-hairsoft flex w-full items-center gap-2.5 rounded-full px-3 py-2 text-start"
              >
                <Clapperboard
                  className={cn("size-4 flex-none", isActive ? "text-ink" : "text-n600")}
                />
                <span className="text-ink min-w-0 flex-1 truncate text-sm">{m.name}</span>
                {/* The tier, not the channel: what a switch costs is the only
                    thing a reader can act on — the wire channel is our problem. */}
                {m.tier && <span className="text-n600 text-2xs flex-none">{m.tier}</span>}
                {isActive && activeResolution && (
                  <span className="text-n600 text-2xs flex-none">{activeResolution}</span>
                )}
                {isActive && !hasSubmenu && (
                  <Check className="text-ink size-3.5 flex-none" strokeWidth={2.4} />
                )}
                {hasSubmenu && <ChevronRight className="text-n600 size-3.5 flex-none" />}
              </button>

              {hasSubmenu && expanded === m.id && (
                <div className="border-hairline bg-paper absolute end-full top-0 me-1 w-32 rounded-2xl border p-1 shadow-lg">
                  {tiers.map((tier) => (
                    <button
                      key={tier}
                      type="button"
                      onClick={() => choose(m, tier)}
                      className="hover:bg-hairsoft flex w-full items-center gap-2 rounded-full px-3 py-2 text-start"
                    >
                      <span className="text-ink min-w-0 flex-1 text-sm">{tier}</span>
                      {isActive && tier === activeResolution && (
                        <Check className="text-ink size-3.5 flex-none" strokeWidth={2.4} />
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </Menu>
      <button
        type="button"
        onClick={() => (open ? close() : setOpen(true))}
        title={t("videoModel.pick")}
        className="hover:bg-hairsoft flex h-8 items-center gap-2 rounded-full px-3"
      >
        <Clapperboard className="text-ink size-4" />
        <span className="text-ink text-sm font-medium">{activeName}</span>
        {activeResolution && <span className="text-n600 text-2xs">{activeResolution}</span>}
        <span className="text-n600 text-2xs">▾</span>
      </button>
    </div>
  )
}

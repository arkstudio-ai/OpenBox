import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, Clapperboard } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { Menu } from "@/shared/ui/Menu"
import type { VideoModelInfo } from "@/shared/types/api"

interface Props {
  models: VideoModelInfo[]
  activeId?: string
  onPick: (id: string) => void
}

/** The composer's video-model pill, beside the chat-model one.
 *
 *  A single shared icon rather than per-vendor logos: these are all served
 *  through the same gateway, and the row that matters to the reader is the
 *  price tier, not the badge.
 */
export function VideoModelPicker({ models, activeId, onPick }: Props) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  // A conversation can be pinned to a model no longer offered — retired, or
  // dropped from allowed_models since. Show the id rather than nothing.
  const active = models.find((m) => m.id === activeId)
  const activeName = active?.name ?? activeId ?? ""

  if (models.length === 0) return null

  return (
    <div className="relative">
      <Menu open={open} onClose={() => setOpen(false)} className="end-0 bottom-10 w-64">
        {models.map((m) => (
          <button
            key={m.id}
            type="button"
            onClick={() => {
              onPick(m.id)
              setOpen(false)
            }}
            className="hover:bg-hairsoft flex items-center gap-2.5 rounded-full px-3 py-2 text-start"
          >
            <Clapperboard
              className={cn("size-4 flex-none", m.id === activeId ? "text-ink" : "text-n600")}
            />
            <span className="text-ink min-w-0 flex-1 truncate text-sm">{m.name}</span>
            {/* The tier, not the channel: what a switch costs is the only thing
                a reader can act on — the wire channel is our problem. */}
            {m.tier && <span className="text-n600 text-2xs flex-none">{m.tier}</span>}
            {m.id === activeId && <Check className="text-ink size-3.5 flex-none" strokeWidth={2.4} />}
          </button>
        ))}
      </Menu>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={t("videoModel.pick")}
        className="hover:bg-hairsoft flex h-8 items-center gap-2 rounded-full px-3"
      >
        <Clapperboard className="text-ink size-4" />
        <span className="text-ink text-sm font-medium">{activeName}</span>
        <span className="text-n600 text-2xs">▾</span>
      </button>
    </div>
  )
}

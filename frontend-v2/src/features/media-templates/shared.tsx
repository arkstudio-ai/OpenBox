import { Image as ImageIcon, Film } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import type { Media, Template } from "./api"

export const field = "border-hair bg-bg text-ink min-h-9 w-full rounded-lg border px-3 py-2 text-sm"
export const button =
  "border-hair bg-card hover:bg-hairsoft text-ink inline-flex min-h-9 items-center justify-center gap-1.5 rounded-full border px-4 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
export const primary = cn(button, "bg-ink text-bg hover:bg-n800 hover:text-bg border-transparent")

export function useMediaText() {
  const { t, i18n } = useTranslation("skills")
  return {
    tx: (key: string, values?: Record<string, string | number>) => t(`mediaTemplates.${key}`, values),
    title: (row: Template) => row.title_translations?.[i18n.language] ?? row.title,
  }
}

export function Preview({ media, thumbnail = false }: { media?: Media | null; thumbnail?: boolean }) {
  const { tx } = useMediaText()
  const video = media?.mime?.startsWith("video/")
  const playable = video && !thumbnail && Boolean(media?.media_url)
  const url = thumbnail
    ? media?.poster_url || (video ? null : media?.media_url)
    : media?.media_url || media?.poster_url
  const placeholder = video ? <Film size={22} /> : <ImageIcon size={22} />
  if (!url)
    return (
      <div
        className="bg-hairsoft text-n500 flex min-h-20 items-center justify-center rounded-lg"
        title={tx("unavailable")}
      >
        {placeholder}
      </div>
    )
  return playable ? (
    <video
      controls
      preload="metadata"
      poster={media?.poster_url ?? undefined}
      src={url}
      aria-label={media?.name || tx("preview")}
      className="bg-n900 max-h-96 w-full rounded-lg"
    />
  ) : (
    <img
      src={url}
      loading="lazy"
      alt={media?.name || ""}
      className={cn("w-full rounded-lg", thumbnail ? "aspect-video object-cover" : "max-h-96 object-contain")}
    />
  )
}

// Files of a conversation shown where they were said: the images a person
// attached, the pictures and videos the assistant produced. Each one is read
// through a short-lived signed link the worker audits, asked for only once the
// file is on screen; a file that cannot be linked stays visible by name.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ExternalLink, FileText } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { formatBytes } from "@/shared/lib/format"
import { mediaKindOf } from "../../components/inspector/media"
import { useOnScreen } from "../../components/inspector/useOnScreen"
import type { Seq } from "../../types/protocol"
import { useMediaLink } from "../api"
import type { ReplayEvent } from "../types"

export type ArtifactEvent = Extract<ReplayEvent, { type: "artifact" }>

const TILE = "border-hair bg-hairsoft overflow-hidden rounded-xl border"

function FileChip({ event, href, note }: { event: ArtifactEvent; href?: string; note?: string }) {
  const { t } = useTranslation("admin-trajectories")
  const name = event.name || event.title || t("common.dash")
  const size = typeof event.size_bytes === "number" ? formatBytes(event.size_bytes) : null
  const body = (
    <>
      <FileText size={14} aria-hidden className="flex-none" />
      <span className="min-w-0 break-all">{name}</span>
      {size && <span className="text-n500 flex-none">{size}</span>}
      {note && <span className="text-n500 flex-none">{note}</span>}
      {href && <ExternalLink size={11} aria-hidden className="flex-none" />}
    </>
  )
  const className =
    "border-hair text-n700 inline-flex max-w-full items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs"
  return href ? (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(className, "hover:bg-hairsoft")}
      title={t("lite.replay.media.open")}
      data-testid="replay-file"
    >
      {body}
    </a>
  ) : (
    <span className={className} data-testid="replay-file">
      {body}
    </span>
  )
}

/** One file: a picture, a player, or its name when it is neither or cannot be read. */
export function ReplayFileView({
  event,
  sessionId,
  throughSeq,
}: {
  event: ArtifactEvent
  sessionId: string
  throughSeq: Seq
}) {
  const { t } = useTranslation("admin-trajectories")
  const [node, setNode] = useState<HTMLDivElement | null>(null)
  const onScreen = useOnScreen(node)
  // The link is asked for when the file first comes on screen; the answer stays while the file is mounted,
  // so scrolling past a playing video does not drop it.
  const readable =
    !!event.payload_id && !!event.linkable && (event.availability ?? "available") === "available"
  const link = useMediaLink(sessionId, readable ? (event.payload_id ?? null) : null, throughSeq, onScreen)
  const kind = mediaKindOf(event.media_type ?? "")
  const name = event.name || event.title || ""
  const url = link.data?.url

  let content
  if (!readable) {
    const gone = !!event.availability && event.availability !== "available"
    content = <FileChip event={event} note={gone ? t("lite.replay.media.unavailable") : undefined} />
  } else if (link.error) {
    content = <FileChip event={event} note={t("lite.replay.media.unavailable")} />
  } else if (!url) {
    content = kind ? (
      <div
        className={cn(TILE, "size-36 animate-pulse")}
        role="status"
        aria-label={t("lite.replay.media.loading")}
        data-testid="replay-media-loading"
      />
    ) : (
      <FileChip event={event} />
    )
  } else if (kind === "image") {
    content = (
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        title={name || t("lite.replay.media.open")}
        className={cn(TILE, "block")}
      >
        <img
          src={url}
          alt={name}
          loading="lazy"
          className="max-h-64 max-w-full object-contain"
          data-testid="replay-media"
        />
      </a>
    )
  } else if (kind === "video") {
    content = (
      <video
        src={url}
        controls
        preload="metadata"
        aria-label={name}
        className={cn(TILE, "max-h-80 max-w-full bg-black")}
        data-testid="replay-media"
      />
    )
  } else if (kind === "audio") {
    content = <audio src={url} controls aria-label={name} className="max-w-full" data-testid="replay-media" />
  } else {
    content = <FileChip event={event} href={url} />
  }
  return (
    <div ref={setNode} className="max-w-full min-w-0" data-role={event.role ?? ""}>
      {content}
    </div>
  )
}

/** The files of one moment of a turn, side by side; the person's own sit on their side of the conversation. */
export function ReplayFiles({
  events,
  sessionId,
  throughSeq,
  side = "start",
}: {
  events: ArtifactEvent[]
  sessionId: string
  throughSeq: Seq
  side?: "start" | "end"
}) {
  const { t } = useTranslation("admin-trajectories")
  if (!events.length) return null
  return (
    <div
      className={cn("flex flex-wrap items-end gap-2", side === "end" && "justify-end")}
      aria-label={side === "end" ? t("lite.replay.media.attached") : t("lite.replay.media.produced")}
      data-testid="replay-files"
    >
      {events.map((event) => (
        <ReplayFileView key={event.record_id} event={event} sessionId={sessionId} throughSeq={throughSeq} />
      ))}
    </div>
  )
}

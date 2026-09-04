// The turn's work log: the tool-step prose and the evidence it produced, in
// the order they happened.
//
// This reads in the answer's own column and stays open. It used to sit behind
// a collapsed trace row, which hid the only account of what the turn actually
// did — and because `finalMessageIndex` moves prose between "final" and
// "progress" while a turn streams, a paragraph already on screen would drop
// into the folded row the moment a tool part landed, reading as if it had been
// lost. Open and inline, the narration simply accumulates above the answer.
import { lazy, Suspense, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import type { ArtifactGroup, WorkEvent } from "../lib/content-view"
import { isGalleryMedia } from "../lib/media"
import { AttachmentGallery } from "./AttachmentGallery"

const Markdown = lazy(() => import("./Markdown"))
const MAX_COMPUTER_CHECKPOINTS = 3

function keyComputerIds(events: WorkEvent[]): Set<string> {
  const screenshots = events.filter(
    (event): event is ArtifactGroup =>
      event.kind === "artifact" && event.artifactKind === "computer_screenshot",
  )
  if (screenshots.length <= MAX_COMPUTER_CHECKPOINTS) {
    return new Set(screenshots.map((item) => item.id))
  }
  const middle = Math.round((screenshots.length - 1) / 2)
  return new Set([screenshots[0].id, screenshots[middle].id, screenshots.at(-1)!.id])
}

interface Props {
  events: WorkEvent[]
  streaming: boolean
}

export function WorkLogTrace({ events, streaming }: Props) {
  const { t } = useTranslation("chat")
  const keyIds = useMemo(() => keyComputerIds(events), [events])
  const displayed = events.filter(
    (event) =>
      event.kind !== "artifact" || event.artifactKind !== "computer_screenshot" || keyIds.has(event.id),
  )
  const hiddenScreenshots = events.length - displayed.length

  if (events.length === 0) return null

  const title = streaming ? t("trace.work.titleActive") : t("trace.work.titleDone")

  return (
    <section aria-label={title} className="mb-3">
      <div className={cn("mb-1 text-xs font-medium", streaming ? "text-shimmer" : "text-n600")}>
        {title}
      </div>
      <ol className="space-y-2">
        {displayed.map((event, index) => (
          <li key={event.id} className="grid grid-cols-[0.875rem_minmax(0,1fr)] gap-x-2 text-xs leading-5">
            <div className="relative flex justify-center">
              {index > 0 && <span className="bg-hair absolute -top-2 bottom-1/2 w-px" />}
              {index < displayed.length - 1 && <span className="bg-hair absolute top-1/2 -bottom-2 w-px" />}
              <span className="bg-n500 ring-bg relative z-10 mt-[0.45rem] size-1.5 rounded-full ring-4" />
            </div>
            {event.kind === "narration" ? (
              <div className="text-n700 min-w-0 pb-1">
                <Suspense fallback={<span className="whitespace-pre-wrap">{event.text}</span>}>
                  <Markdown text={event.text} streaming={streaming} variant="thinking" />
                </Suspense>
              </div>
            ) : (
              <div className="min-w-0 pb-2">
                <div className="text-n700 mb-1 truncate font-medium">
                  {event.sourceTool?.title || event.label || t("trace.work.checkpoint")}
                </div>
                <AttachmentGallery parts={event.parts.filter(isGalleryMedia)} className="max-w-110" compact />
              </div>
            )}
          </li>
        ))}
      </ol>
      {hiddenScreenshots > 0 ? (
        <p className={cn("text-n600 text-2xs mt-1", displayed.length > 0 && "ms-5.5")}>
          {t("trace.work.omitted", { count: hiddenScreenshots })}
        </p>
      ) : null}
    </section>
  )
}

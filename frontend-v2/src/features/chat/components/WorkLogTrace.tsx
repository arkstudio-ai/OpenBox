import { lazy, Suspense, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import type { ArtifactGroup, WorkEvent } from "../lib/content-view"
import { isGalleryMedia } from "../lib/media"
import { AttachmentGallery } from "./AttachmentGallery"
import { TraceShell } from "./TraceShell"

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
  autoCollapseReady: boolean
  defaultOpen?: boolean
}

export function WorkLogTrace({ events, streaming, autoCollapseReady, defaultOpen }: Props) {
  const { t } = useTranslation("chat")
  const keyIds = useMemo(() => keyComputerIds(events), [events])
  const narrationCount = events.filter((event) => event.kind === "narration").length
  const evidence = events.filter((event): event is ArtifactGroup => event.kind === "artifact")
  const displayed = events.filter(
    (event) =>
      event.kind !== "artifact" || event.artifactKind !== "computer_screenshot" || keyIds.has(event.id),
  )
  const hiddenScreenshots = events.length - displayed.length

  if (events.length === 0) return null
  return (
    <TraceShell
      title={streaming ? t("trace.work.titleActive") : t("trace.work.titleDone")}
      subtitle={t("trace.work.summary", {
        messages: narrationCount,
        screenshots: evidence.length,
      })}
      streaming={streaming}
      autoCollapseReady={autoCollapseReady}
      defaultOpen={defaultOpen}
    >
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
    </TraceShell>
  )
}

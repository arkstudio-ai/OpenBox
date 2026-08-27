import { CheckCircle2, CircleAlert, FileArchive, Film, ImageIcon, MonitorCheck } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import type { ArtifactGroup } from "../lib/content-view"
import { isAudioPart, isGalleryMedia } from "../lib/media"
import { AudioPreview } from "./AudioPreview"
import { AttachmentGallery } from "./AttachmentGallery"
import { FileChip } from "./PatchChip"

function metadataString(group: ArtifactGroup, key: string): string | null {
  const value = group.metadata[key]
  return typeof value === "string" && value.trim() ? value.trim() : null
}

function metadataNumber(group: ArtifactGroup, key: string): number | null {
  const value = group.metadata[key]
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function GroupTitle({ group, segmentNumber }: { group: ArtifactGroup; segmentNumber?: number }) {
  const { t } = useTranslation("chat")
  if (group.artifactKind === "video_final") return <>{group.label || t("artifacts.finalVideo")}</>
  if (group.artifactKind === "video_segment") {
    const number = group.ordinal ?? segmentNumber
    return <>{number ? t("artifacts.segment", { number }) : t("artifacts.videoSegment")}</>
  }
  if (group.artifactKind === "generated_image") {
    return <>{t("artifacts.generatedImages", { count: group.parts.length })}</>
  }
  return <>{group.label || t("artifacts.result")}</>
}

function QaBadge({ group }: { group: ArtifactGroup }) {
  const { t } = useTranslation("chat")
  const verdict = metadataString(group, "stt_verdict")
  const similarity = metadataNumber(group, "stt_similarity")
  if (!verdict) return null
  const ok = verdict === "ok"
  return (
    <span
      className={cn(
        "text-2xs inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium",
        ok ? "bg-s100 text-s700" : "bg-dangersoft text-dangerink",
      )}
    >
      {ok ? <CheckCircle2 className="size-3" /> : <CircleAlert className="size-3" />}
      {ok ? t("artifacts.sttOk") : t("artifacts.sttReview")}
      {similarity != null ? ` · ${Math.round(similarity * 100)}%` : ""}
    </span>
  )
}

function ArtifactCard({
  group,
  hero = false,
  segmentNumber,
}: {
  group: ArtifactGroup
  hero?: boolean
  segmentNumber?: number
}) {
  const { t } = useTranslation("chat")
  const media = group.parts.filter(isGalleryMedia)
  const audio = group.parts.filter((part) => isAudioPart(part) && Boolean(part.asset_id))
  const files = group.parts.filter(
    (part) => !isGalleryMedia(part) && !(isAudioPart(part) && Boolean(part.asset_id)),
  )
  const transcript = metadataString(group, "transcript")
  return (
    <section className={cn("border-hair bg-card/45 min-w-0 rounded-xl border", hero ? "p-4" : "p-3")}>
      <div className="mb-2 flex min-w-0 items-center gap-2">
        <span className="bg-n200 flex size-7 shrink-0 items-center justify-center rounded-full">
          {group.artifactKind === "generated_image" ? (
            <ImageIcon className="text-n700 size-3.5" />
          ) : group.artifactKind === "shared_file" ? (
            <FileArchive className="text-n700 size-3.5" />
          ) : (
            <Film className="text-n700 size-3.5" />
          )}
        </span>
        <h3 className="text-ink min-w-0 flex-1 truncate text-sm font-medium">
          <GroupTitle group={group} segmentNumber={segmentNumber} />
        </h3>
        {group.revision && group.revision > 1 ? (
          <span className="text-n600 bg-n200 text-2xs rounded-full px-2 py-0.5">
            {t("artifacts.revision", { number: group.revision })}
          </span>
        ) : null}
        <QaBadge group={group} />
      </div>
      {group.caption ? (
        <div className="text-n700 mb-2 text-sm leading-6 [overflow-wrap:anywhere]">
          {group.artifactKind === "generated_image" ? (
            <span className="text-n600 me-1.5 text-xs">{t("artifacts.prompt")}</span>
          ) : null}
          {group.caption}
        </div>
      ) : null}
      {transcript && transcript !== group.caption ? (
        <details className="mb-2">
          <summary className="text-n600 hover:text-ink cursor-pointer text-xs">
            {t("artifacts.transcript")}
          </summary>
          <p className="text-n700 mt-1 text-xs leading-5 [overflow-wrap:anywhere]">{transcript}</p>
        </details>
      ) : null}
      <AttachmentGallery parts={media} hero={hero || group.artifactKind === "video_final"} compact={!hero} />
      {audio.length > 0 ? (
        <div className="space-y-2">
          {audio.map((part) => (
            <AudioPreview key={part.id} part={part} />
          ))}
        </div>
      ) : null}
      {files.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {files.map((part) => (
            <FileChip key={part.id} part={part} />
          ))}
        </div>
      ) : null}
    </section>
  )
}

function VerificationCard({ group }: { group: ArtifactGroup }) {
  const { t } = useTranslation("chat")
  return (
    <section className="border-hair bg-card/35 mt-3 rounded-xl border p-3">
      <div className="text-n700 mb-2 flex items-center gap-2 text-xs font-medium">
        <MonitorCheck className="size-4" />
        {t("artifacts.verification")}
      </div>
      <AttachmentGallery parts={group.parts.filter(isGalleryMedia)} hero />
    </section>
  )
}

export function ResultArtifacts({
  groups,
  verification,
}: {
  groups: ArtifactGroup[]
  verification: ArtifactGroup | null
}) {
  const { t } = useTranslation("chat")
  const finals = groups.filter((group) => group.role === "final")
  const segments = groups.filter((group) => group.artifactKind === "video_segment")
  const ordinary = groups.filter((group) => group.role !== "final" && group.artifactKind !== "video_segment")
  if (groups.length === 0 && !verification) return null

  return (
    <div className="mt-3 flex flex-col gap-3">
      {finals.map((group) => (
        <ArtifactCard key={group.id} group={group} hero />
      ))}
      {ordinary.map((group) => (
        <ArtifactCard key={group.id} group={group} />
      ))}
      {segments.length > 0 ? (
        <section>
          <div className="text-n600 mb-2 text-xs font-medium">
            {t("artifacts.segmentCollection", { count: segments.length })}
          </div>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            {segments.map((group, index) => (
              <ArtifactCard key={group.id} group={group} segmentNumber={index + 1} />
            ))}
          </div>
        </section>
      ) : null}
      {verification ? <VerificationCard group={verification} /> : null}
    </div>
  )
}

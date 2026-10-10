// Pure media-part predicates for the attachment gallery. No React, no I/O.
import type { FilePart } from "@/shared/types/api"

/** Use stored provenance or the artifact projection's verified origin. A
 * share_file copy can be a proven video_final while its stored kind stays
 * shared_file. Merely being a final/large attachment is not evidence. */
export function isGeneratedMedia(part: FilePart, artifactKind?: string): boolean {
  if (part.transient || part.relation?.role === "evidence") return false
  const generatedKinds = ["generated_image", "video_segment", "video_final", "generated_audio"]
  return generatedKinds.includes(part.relation?.kind ?? "") || generatedKinds.includes(artifactKind ?? "")
}

/** Media the gallery can preview: uploaded assets with an image or video
 *  mime. Videos paint their first frame under a play badge; anything else
 *  stays a filename chip. */
export function isGalleryMedia(part: { asset_id?: string; mime_type?: string }): boolean {
  return (
    Boolean(part.asset_id) &&
    Boolean(part.mime_type?.startsWith("image/") || part.mime_type?.startsWith("video/"))
  )
}

export function isVideoPart(part: { mime_type?: string }): boolean {
  return Boolean(part.mime_type?.startsWith("video/"))
}

export function isAudioPart(part: { mime_type?: string }): boolean {
  return Boolean(part.mime_type?.startsWith("audio/"))
}

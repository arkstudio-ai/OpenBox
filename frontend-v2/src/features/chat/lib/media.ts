// Pure media-part predicates for the attachment gallery. No React, no I/O.

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

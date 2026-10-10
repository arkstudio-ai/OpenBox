/** Older replies pasted the tool's `/api/assets/<id>/download?token=…` link.
 *  The token died after a day; the asset did not. Clients trade the asset id
 *  for a fresh signed URL on click instead of navigating to a 401. */
const ASSET_DOWNLOAD =
  /^(?:https?:\/\/[^/]+)?\/api\/assets\/(asset_[A-Za-z0-9]+)\/(?:download|url)(?:[?#].*)?$/

export function assetIdFromLink(href: string | undefined): string | null {
  if (typeof href !== "string") return null
  return ASSET_DOWNLOAD.exec(href)?.[1] ?? null
}

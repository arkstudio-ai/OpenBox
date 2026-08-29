import { useTranslation } from "react-i18next"
import { useAssetUrl } from "@/shared/api/assets"

export interface PreviewableAsset {
  assetId: string
  name: string
  mime?: string
}

/** One produced file, shown rather than named.
 *
 *  A job that spent real money and minutes to make a video has to hand it
 *  over: both surfaces used to print the filename as inert text, so the only
 *  way to see the result was to go hunting in the resource centre. Lives in
 *  shared because the live job card and the durable chat receipt both need
 *  it, and features must not import each other (§4.1).
 *
 *  Video and images preview in place; anything else gets a link.
 */
export function AssetPreview({ artifact }: { artifact: PreviewableAsset }) {
  const { t } = useTranslation("jobs")
  const asset = useAssetUrl(artifact.assetId)
  const url = asset.data?.url
  const mime = artifact.mime || asset.data?.mime || ""

  if (!url) {
    return (
      <div className="text-n600 mt-1 truncate text-xs">
        {artifact.name}
        {asset.isError && ` · ${t("artifact.unavailable")}`}
      </div>
    )
  }

  if (mime.startsWith("video/")) {
    return (
      <figure className="mt-2">
        {/* Muted by default: several finished segments can land in one dock,
            and none of them should start making noise on their own. */}
        <video
          src={url}
          controls
          muted
          playsInline
          preload="metadata"
          className="border-hair max-h-64 w-full rounded-lg border bg-black"
        />
        <figcaption className="mt-1 truncate">
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="text-n600 hover:text-ink text-xs underline-offset-2 hover:underline"
          >
            {artifact.name}
          </a>
        </figcaption>
      </figure>
    )
  }

  if (mime.startsWith("image/")) {
    return (
      <a href={url} target="_blank" rel="noreferrer" className="mt-2 block">
        <img
          src={url}
          alt={artifact.name}
          className="border-hair max-h-64 w-full rounded-lg border object-contain"
        />
      </a>
    )
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="text-n600 hover:text-ink mt-1 block truncate text-xs underline-offset-2 hover:underline"
    >
      {artifact.name}
    </a>
  )
}

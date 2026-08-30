import { useTranslation } from "react-i18next"
import { useAssetUrl } from "@/shared/api/assets"

export interface PreviewableAsset {
  assetId?: string
  name?: string
  mime?: string
}

/** One produced file, shown rather than named.
 *
 *  A job that spent real money and minutes to make a video has to hand it
 *  over: historical receipts must keep the result accessible without relying
 *  on the retired live-job API. This transport-level preview stays in shared
 *  so the receipt can resolve an asset directly from its stored part data.
 *
 *  Video and images preview in place; anything else gets a link.
 */
export function AssetPreview({ artifact }: { artifact: PreviewableAsset }) {
  const { t } = useTranslation("common")
  const asset = useAssetUrl(artifact.assetId)
  const url = asset.data?.url
  const mime = artifact.mime || asset.data?.mime || ""
  const unavailableLabel = t("state.unavailable")
  const name = artifact.name?.trim() || asset.data?.name || artifact.assetId || unavailableLabel
  const unavailable = !artifact.assetId || asset.isError || (asset.isSuccess && !url)

  if (!url) {
    return (
      <div className="text-n600 mt-1 truncate text-xs">
        {name}
        {unavailable && name !== unavailableLabel && ` · ${unavailableLabel}`}
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
            {name}
          </a>
        </figcaption>
      </figure>
    )
  }

  if (mime.startsWith("image/")) {
    return (
      <a href={url} target="_blank" rel="noreferrer" className="mt-2 block">
        <img src={url} alt={name} className="border-hair max-h-64 w-full rounded-lg border object-contain" />
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
      {name}
    </a>
  )
}

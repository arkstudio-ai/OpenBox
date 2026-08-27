import type { FilePart } from "@/shared/types/api"
import { useAssetUrl } from "../api/assets"

export function AudioPreview({ part }: { part: FilePart }) {
  const { data } = useAssetUrl(part.asset_id)
  if (!data?.url) return null
  return <audio src={data.url} controls preload="metadata" className="h-10 w-full max-w-full" />
}

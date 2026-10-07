import type { FilePart } from "@/shared/types/api"
import { useAssetUrl } from "../api/assets"
import { isGeneratedMedia } from "../lib/media"
import { AiGeneratedLabel } from "./AiDisclosure"

export function AudioPreview({ part }: { part: FilePart }) {
  const { data } = useAssetUrl(part.asset_id)
  if (!data?.url) return null
  return (
    <div className="space-y-1">
      {isGeneratedMedia(part) && <AiGeneratedLabel />}
      <audio src={data.url} controls preload="metadata" className="h-10 w-full max-w-full" />
    </div>
  )
}

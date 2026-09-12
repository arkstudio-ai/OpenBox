import { useCallback } from "react"
import type { MediaKind } from "./media"

interface MediaElementProps {
  blob: Blob
  kind: MediaKind
  label: string
}

/**
 * Protected media. The object URL lives exactly as long as the element for
 * this blob: it is created in the ref callback and revoked in that callback's
 * cleanup, which React runs when the blob changes, when the element unmounts
 * (deletion, refusal, target switch) and around StrictMode double mounts. No
 * URL is created during render.
 */
export function MediaElement({ blob, kind, label }: MediaElementProps) {
  const attach = useCallback(
    (element: HTMLImageElement | HTMLMediaElement | null) => {
      if (!element) return
      const url = URL.createObjectURL(blob)
      element.src = url
      return () => {
        element.removeAttribute("src")
        URL.revokeObjectURL(url)
      }
    },
    [blob],
  )
  if (kind === "image")
    return (
      <img
        ref={attach}
        alt={label}
        className="max-h-96 max-w-full rounded-lg object-contain"
        data-testid="trajectory-media"
      />
    )
  if (kind === "audio")
    return (
      <audio ref={attach} controls aria-label={label} className="w-full" data-testid="trajectory-media" />
    )
  return (
    <video
      ref={attach}
      controls
      aria-label={label}
      className="max-h-96 w-full rounded-lg"
      data-testid="trajectory-media"
    />
  )
}

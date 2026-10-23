import { useState } from "react"
import { cn } from "@/shared/lib/cn"
import type { CompanionAppearance } from "./companion"
import { companionPortrait, DEFAULT_COMPANION_PRESET } from "./companion-presets"

interface Props {
  appearance: Pick<CompanionAppearance, "preset" | "image_url">
  className?: string
  loading?: "eager" | "lazy"
}

/** The same transparent portrait in settings, conversations and the floating companion. */
export function CompanionAvatar({ appearance, className, loading }: Props) {
  const [failedUrls, setFailedUrls] = useState<string[]>([])
  const portrait = companionPortrait(appearance.preset)
  const source = appearance.preset === "custom" && appearance.image_url ? appearance.image_url : portrait
  const fallback = companionPortrait(DEFAULT_COMPANION_PRESET)
  const src = failedUrls.includes(source) ? fallback : source
  return (
    <span
      aria-hidden
      className={cn("relative inline-flex aspect-square shrink-0 items-center justify-center", className)}
    >
      {!failedUrls.includes(src) && (
        <img
          src={src}
          alt=""
          width={512}
          height={512}
          loading={loading}
          decoding="async"
          draggable={false}
          onError={() => setFailedUrls((urls) => (urls.includes(src) ? urls : [...urls, src]))}
          className="size-full object-contain"
        />
      )}
    </span>
  )
}

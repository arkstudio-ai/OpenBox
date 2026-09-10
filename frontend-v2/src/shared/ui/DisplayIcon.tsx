import { useState } from "react"

/** Untrusted catalogue icons are text or HTTPS images, never HTML/SVG markup. */
export function DisplayIcon({
  icon,
  className = "size-6",
  fallback = "🧩",
}: {
  icon?: string | null
  className?: string
  fallback?: string
}) {
  const [failed, setFailed] = useState<string | null>(null)
  const image = /^https:\/\/[^\s]+$/i.test(icon ?? "")
  return (
    <span
      aria-hidden
      className={`inline-flex shrink-0 items-center justify-center overflow-hidden rounded-md ${className}`}
    >
      {image && failed !== icon ? (
        <img
          src={icon!}
          alt=""
          className="size-full object-contain"
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setFailed(icon!)}
        />
      ) : image ? (
        fallback
      ) : (
        icon || fallback
      )}
    </span>
  )
}

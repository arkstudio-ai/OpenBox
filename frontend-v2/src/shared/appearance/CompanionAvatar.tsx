import { useId, useState } from "react"
import { cn } from "@/shared/lib/cn"
import type { CompanionAppearance } from "./companion"

interface Props {
  appearance: Pick<CompanionAppearance, "preset" | "image_url">
  className?: string
}

/** Original vector characters; custom portraits fall back gracefully if their URL expires. */
export function CompanionAvatar({ appearance, className }: Props) {
  const id = useId()
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const custom = appearance.preset === "custom" && appearance.image_url && appearance.image_url !== failedUrl
  return (
    <span
      aria-hidden
      className={cn("relative inline-flex aspect-square shrink-0 items-center justify-center", className)}
    >
      {custom ? (
        <img
          src={appearance.image_url ?? undefined}
          alt=""
          draggable={false}
          onError={() => setFailedUrl(appearance.image_url)}
          className="size-full object-contain"
        />
      ) : (
        <svg viewBox="0 0 96 96" fill="none" className="size-full overflow-visible">
          <defs>
            <radialGradient id={id} cx="0.32" cy="0.22" r="0.85">
              <stop stopColor="var(--t-card)" />
              <stop
                offset="0.45"
                stopColor={appearance.preset === "sprout" ? "var(--t-s300)" : "var(--t-a300)"}
              />
              <stop
                offset="1"
                stopColor={appearance.preset === "sprout" ? "var(--t-s600)" : "var(--t-accent)"}
              />
            </radialGradient>
          </defs>
          <ellipse cx="48" cy="87" rx="27" ry="4" fill="var(--t-ink)" opacity="0.1" />
          {appearance.preset === "cat" && (
            <path
              d="M18 42 19 12 38 24M58 24 77 12 78 42"
              fill={`url(#${id})`}
              stroke="var(--t-accent)"
              strokeWidth="2"
              strokeLinejoin="round"
            />
          )}
          {appearance.preset === "sprout" && (
            <>
              <path d="M48 30C47 13 55 6 67 8 69 20 61 29 48 30Z" fill="var(--t-s600)" />
              <path d="M47 27C46 15 35 9 26 14 26 26 36 31 47 27Z" fill="var(--t-s400)" />
            </>
          )}
          <path
            d={
              appearance.preset === "sprout"
                ? "M15 54C15 31 28 25 48 28 68 25 81 31 81 54 81 73 67 84 48 84 29 84 15 73 15 54Z"
                : "M11 49C11 26 25 13 48 13 71 13 85 26 85 49 85 71 71 84 48 84 25 84 11 71 11 49Z"
            }
            fill={`url(#${id})`}
          />
          <ellipse cx="34" cy="49" rx="3.8" ry="5" fill="var(--t-ink)" />
          <ellipse cx="62" cy="49" rx="3.8" ry="5" fill="var(--t-ink)" />
          <path
            d={
              appearance.preset === "cat"
                ? "m44 59 4 3 4-3m-4 3c-2 5-7 5-9 2m9-2c2 5 7 5 9 2"
                : "M42 61Q48 67 54 61"
            }
            stroke="var(--t-ink)"
            strokeWidth="2.5"
            strokeLinecap="round"
          />
          <ellipse cx="24" cy="59" rx="5" ry="2.5" fill="var(--t-accent)" opacity="0.25" />
          <ellipse cx="72" cy="59" rx="5" ry="2.5" fill="var(--t-accent)" opacity="0.25" />
          <ellipse
            cx="35"
            cy="29"
            rx="12"
            ry="5"
            transform="rotate(-25 35 29)"
            fill="var(--t-card)"
            opacity="0.4"
          />
        </svg>
      )}
    </span>
  )
}

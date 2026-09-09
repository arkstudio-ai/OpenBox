// The icon column shared by every row in the centre.
//
// Emoji and operator-configured HTTPS images share one safe display helper.
// Missing icons fall back to a stable tinted initial; broken images to a puzzle.
import { DisplayIcon } from "@/shared/ui/DisplayIcon"

const TINTS = [
  "bg-a200 text-n800",
  "bg-s100 text-sage",
  "bg-n200 text-n700",
  "bg-dangersoft text-dangerink",
  "bg-s300 text-n800",
  "bg-a100 text-n700",
] as const

function tintFor(seed: string): string {
  let hash = 0
  for (let i = 0; i < seed.length; i += 1) hash = (hash * 31 + seed.charCodeAt(i)) >>> 0
  return TINTS[hash % TINTS.length]
}

export function EntryIcon({ icon, name, size = "md" }: { icon?: string; name: string; size?: "sm" | "md" }) {
  const box = size === "sm" ? "size-8 text-base" : "size-10 text-xl"

  if (icon) {
    return (
      <span
        className={`flex ${box} bg-hairsoft flex-none items-center justify-center rounded-xl leading-none`}
        aria-hidden
      >
        <DisplayIcon icon={icon} className={box} />
      </span>
    )
  }

  const initial = (name.trim()[0] ?? "?").toUpperCase()
  return (
    <span
      className={`flex ${box} flex-none items-center justify-center rounded-xl leading-none font-medium ${tintFor(name)}`}
      aria-hidden
    >
      {initial}
    </span>
  )
}

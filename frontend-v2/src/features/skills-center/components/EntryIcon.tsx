// The icon column shared by every row in the centre.
//
// Icons are emoji rather than image assets: they need no upload path, no
// serving route and no cache busting, and they survive the sandbox → backend →
// browser hop as plain text. A skill that declares none still needs to be
// distinguishable at a glance, so it falls back to its initial over a tint
// derived from the name — stable per skill, and never a blank square.
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

export function EntryIcon({
  icon,
  name,
  size = "md",
}: {
  icon?: string
  name: string
  size?: "sm" | "md"
}) {
  const box = size === "sm" ? "size-8 text-base" : "size-10 text-xl"

  if (icon) {
    return (
      <span
        className={`flex ${box} flex-none items-center justify-center rounded-xl bg-hairsoft leading-none`}
        aria-hidden
      >
        {icon}
      </span>
    )
  }

  const initial = (name.trim()[0] ?? "?").toUpperCase()
  return (
    <span
      className={`flex ${box} flex-none items-center justify-center rounded-xl font-medium leading-none ${tintFor(name)}`}
      aria-hidden
    >
      {initial}
    </span>
  )
}

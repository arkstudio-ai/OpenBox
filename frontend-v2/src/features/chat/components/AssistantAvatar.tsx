import { cn } from "@/shared/lib/cn"
import { CompanionAvatar } from "@/shared/appearance/CompanionAvatar"
import { DEFAULT_COMPANION, useCompanionAppearance } from "@/shared/appearance/companion"

/** The personal assistant's face: one small mark, the same everywhere it speaks. */
export function AssistantAvatar({ size = "sm", className }: { size?: "sm" | "lg"; className?: string }) {
  const appearance = useCompanionAppearance().data ?? DEFAULT_COMPANION
  return (
    <CompanionAvatar appearance={appearance} className={cn(size === "lg" ? "size-14" : "size-6", className)} />
  )
}

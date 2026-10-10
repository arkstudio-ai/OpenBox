import { Sparkles } from "lucide-react"
import { cn } from "@/shared/lib/cn"

/** The personal assistant's face: one small mark, the same everywhere it speaks. */
export function AssistantAvatar({ size = "sm", className }: { size?: "sm" | "lg"; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "bg-a200 text-accent flex flex-none items-center justify-center rounded-full",
        size === "lg" ? "size-14" : "size-6",
        className,
      )}
    >
      <Sparkles size={size === "lg" ? 26 : 13} strokeWidth={size === "lg" ? 1.8 : 2.2} />
    </span>
  )
}

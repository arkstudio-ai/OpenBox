import { Bot } from "lucide-react"
import { DisplayIcon } from "@/shared/ui/DisplayIcon"
import { cn } from "@/shared/lib/cn"
import type { AgentSpec } from "../types"
import { agentColorOption, agentIconOption } from "../lib/appearance"

export function AgentAvatar({
  display,
  className,
}: {
  display?: AgentSpec["display"] | null
  className?: string
}) {
  const icon = display?.icon || "bot"
  const Icon = agentIconOption(icon)?.Icon ?? (/^[a-z0-9_-]+$/i.test(icon) ? Bot : null)
  return (
    <span
      aria-hidden
      className={cn(
        "border-hair inline-flex size-10 flex-none items-center justify-center overflow-hidden rounded-xl border",
        agentColorOption(display?.color)?.className ?? "bg-hairsoft text-n700",
        className,
      )}
    >
      {Icon ? <Icon className="size-5" /> : <DisplayIcon icon={icon} className="size-6 text-lg" />}
    </span>
  )
}

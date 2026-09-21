import {
  Bot,
  Search,
  FileText,
  ShieldCheck,
  Calculator,
  PenLine,
  Code2,
  Image,
  Mic,
  BookOpen,
  ClipboardCheck,
  CircleCheck,
} from "lucide-react"
import { DisplayIcon } from "@/shared/ui/DisplayIcon"
import { cn } from "@/shared/lib/cn"
import type { AgentSpec } from "../types"

const ICONS = {
  bot: Bot,
  search: Search,
  file: FileText,
  filetext: FileText,
  shield: ShieldCheck,
  shieldcheck: ShieldCheck,
  calculator: Calculator,
  pen: PenLine,
  code: Code2,
  image: Image,
  mic: Mic,
  bookopen: BookOpen,
  clipboardcheck: ClipboardCheck,
  circlecheck: CircleCheck,
  checkcircle: CircleCheck,
}
const COLORS: Record<string, string> = {
  blue: "bg-a100 text-a700",
  amber: "bg-a100 text-a700",
  gold: "bg-a100 text-a700",
  green: "bg-s100 text-sage",
  sage: "bg-s100 text-sage",
  red: "bg-dangersoft text-dangerink",
  gray: "bg-n200 text-n700",
}

export function AgentAvatar({
  display,
  className,
}: {
  display?: AgentSpec["display"] | null
  className?: string
}) {
  const icon = display?.icon || "bot"
  const key = icon.replace(/[-_]/g, "").toLowerCase()
  const Icon = ICONS[key as keyof typeof ICONS] ?? (/^[a-z0-9_-]+$/i.test(icon) ? Bot : null)
  return (
    <span
      aria-hidden
      className={cn(
        "border-hair inline-flex size-10 flex-none items-center justify-center overflow-hidden rounded-xl border",
        COLORS[display?.color ?? ""] ?? "bg-hairsoft text-n700",
        className,
      )}
    >
      {Icon ? <Icon className="size-5" /> : <DisplayIcon icon={icon} className="size-6 text-lg" />}
    </span>
  )
}

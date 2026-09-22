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

/** Shared by the editor and every Agent avatar; values are persisted in display. */
export const AGENT_ICONS = [
  { value: "bot", Icon: Bot },
  { value: "search", Icon: Search },
  { value: "file", Icon: FileText },
  { value: "shield", Icon: ShieldCheck },
  { value: "calculator", Icon: Calculator },
  { value: "pen", Icon: PenLine },
  { value: "code", Icon: Code2 },
  { value: "image", Icon: Image },
  { value: "mic", Icon: Mic },
  { value: "bookopen", Icon: BookOpen },
  { value: "clipboardcheck", Icon: ClipboardCheck },
  { value: "circlecheck", Icon: CircleCheck },
] as const

export const AGENT_COLORS = [
  { value: "blue", className: "bg-agent-blue/12 text-agent-blue", swatch: "bg-agent-blue" },
  { value: "amber", className: "bg-agent-amber/12 text-agent-amber", swatch: "bg-agent-amber" },
  { value: "green", className: "bg-agent-green/12 text-agent-green", swatch: "bg-agent-green" },
  { value: "violet", className: "bg-agent-violet/12 text-agent-violet", swatch: "bg-agent-violet" },
  { value: "red", className: "bg-agent-red/12 text-agent-red", swatch: "bg-agent-red" },
  { value: "gray", className: "bg-agent-gray/12 text-agent-gray", swatch: "bg-agent-gray" },
] as const

const ICON_ALIASES: Record<string, string> = {
  filetext: "file",
  shieldcheck: "shield",
  checkcircle: "circlecheck",
}
const COLOR_ALIASES: Record<string, string> = { gold: "amber", sage: "green" }

export function agentIconOption(value?: string) {
  const key = (value || "bot").replace(/[-_]/g, "").toLowerCase()
  return AGENT_ICONS.find((option) => option.value === (ICON_ALIASES[key] ?? key))
}

export function agentColorOption(value?: string) {
  const key = (value || "blue").toLowerCase()
  return AGENT_COLORS.find((option) => option.value === (COLOR_ALIASES[key] ?? key))
}

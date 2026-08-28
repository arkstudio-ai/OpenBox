// One call per row: what kind it was, what it was aimed at, how it ended.
//
// The detail — arguments, output, diffs — lives behind the row rather than
// under it. A turn makes a dozen calls and each one's request/response block
// runs to a screenful, so rendering them all inline buried the answer under
// its own paperwork. Collapsed, a call costs one line; the reader opens the
// two they care about.
//
// Shared by the flat tool chain and the task card, so a call reads the same
// way wherever it is listed.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronRight } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { formatDuration } from "@/shared/lib/format"
import type { SubtaskPart, ToolPart } from "@/shared/types/api"
import { describeTool, toneBgClass, toneFgClass, toolTarget } from "../lib/tool-map"
import { parseExitCode } from "../lib/tool-parse"
import { toolDuration, type ToolLike } from "../lib/turn-view"
import { SubagentLine } from "./SubagentLine"
import { ToolOutput } from "./tool/ToolOutput"

function Row({ part }: { part: ToolPart | SubtaskPart }) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)

  const subtask = part.type === "subtask"
  const glyph = subtask ? null : describeTool(part.tool)
  const label = subtask ? t("kind.task") : t(`kind.${glyph!.kindKey}`)
  const target = subtask ? part.description : toolTarget(part)

  const running = part.status === "running" || part.status === "pending"
  const failed = part.status === "error"
  const seconds = toolDuration(part)
  const exit = part.type === "tool" ? parseExitCode(part.metadata) : null

  // The right-hand column: how it went, in the fewest words that still say
  // something. A running call has no duration yet, so it says so instead.
  const meta = running
    ? t("toolMeta.running")
    : [
        seconds !== null ? formatDuration(seconds) : null,
        exit !== null ? t("toolMeta.exit", { code: exit }) : failed ? t("toolMeta.failed") : null,
      ]
        .filter(Boolean)
        .join(" · ")

  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="group/row hover:bg-hairsoft/50 -mx-2 flex w-full items-center gap-3 rounded-lg px-2 py-1 text-start transition-colors"
      >
        <span
          className={cn(
            "flex size-6 flex-none items-center justify-center rounded-full text-xs",
            subtask ? toneBgClass.grey : toneBgClass[glyph!.tone],
            subtask ? toneFgClass.grey : toneFgClass[glyph!.tone],
            failed && "text-danger",
          )}
        >
          {subtask ? "◆" : glyph!.glyph}
        </span>
        <span className="text-n700 group-hover/row:text-ink w-20 flex-none truncate text-sm transition-colors max-sm:hidden">
          {label}
        </span>
        <span
          className={cn(
            "min-w-0 flex-1 truncate font-mono text-sm transition-colors",
            failed ? "text-danger" : "text-n800 group-hover/row:text-ink",
          )}
        >
          {target}
        </span>
        <span
          className={cn(
            "text-n600 shrink-0 text-xs tabular-nums max-sm:hidden",
            running && "text-shimmer",
          )}
        >
          {meta}
        </span>
        <ChevronRight
          className={cn(
            "text-n500/60 group-hover/row:text-n600 size-3.5 shrink-0 transition-transform duration-200",
            open && "rotate-90",
          )}
        />
      </button>
      {/* A task's own row says nothing about the minutes the subagent spends
          working, so its progress goes directly under the row rather than
          behind the fold. */}
      {part.type === "tool" && part.tool === "task" && <SubagentLine part={part} />}
      <div className="fold" data-open={open}>
        <div>
          <div className="ps-9 pt-0.5 pb-2">
            <ToolOutput part={part} />
          </div>
        </div>
      </div>
    </li>
  )
}

export function ToolRows({ tools }: { tools: ToolLike[] }) {
  if (tools.length === 0) return null
  return (
    <ul className="flex flex-col">
      {tools.map((part) => (
        <Row key={part.id} part={part} />
      ))}
    </ul>
  )
}

// Tool-chain trace: one collapsed row over the calls the turn made.
//
// The rows are the same ones the task card uses (ToolRows) — a call reads the
// same way wherever it is listed, and its request/response detail stays behind
// the row instead of under it.
import { useTranslation } from "react-i18next"
import type { ToolLike } from "../lib/turn-view"
import { describeTool, toolTarget } from "../lib/tool-map"
import { ToolRows } from "./ToolRows"
import { TraceShell } from "./TraceShell"

/** The call in flight, else the last one that finished. */
function currentCall(tools: ToolLike[]): ToolLike | undefined {
  const running = tools.filter((t) => t.status === "running" || t.status === "pending")
  return running[running.length - 1] ?? tools[tools.length - 1]
}

interface Props {
  tools: ToolLike[]
  streaming: boolean
}

export function ToolChainTrace({ tools, streaming }: Props) {
  const { t } = useTranslation("chat")
  if (tools.length === 0) return null

  // Collapsed, this row is all the reader has, so while the chain runs it
  // names the call rather than counting them: "执行命令 npm test" says more
  // than "3 次工具调用" about a turn that is still going.
  const current = streaming ? currentCall(tools) : undefined
  const subtitle =
    current && current.type === "tool"
      ? `${t(`kind.${describeTool(current.tool).kindKey}`)} ${toolTarget(current)}`
      : current && current.type === "subtask"
        ? `${t("kind.task")} ${current.description}`
        : t("trace.tool.summaryCount", { count: tools.length })

  return (
    <TraceShell
      title={streaming ? t("trace.tool.titleActive") : t("trace.tool.titleDone")}
      subtitle={subtitle}
      streaming={streaming}
    >
      <ToolRows tools={tools} />
    </TraceShell>
  )
}

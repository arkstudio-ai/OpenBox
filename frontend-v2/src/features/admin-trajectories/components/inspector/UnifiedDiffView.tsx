import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { NS } from "./types"

interface UnifiedDiffViewProps {
  diff: string
}

type LineKind = "header" | "hunk" | "add" | "del" | "context"

const CLASSES: Readonly<Record<LineKind, string>> = {
  header: "text-n500",
  hunk: "text-a700 bg-hairsoft",
  add: "bg-diffadd text-ink",
  del: "bg-diffdel text-ink",
  context: "text-n700",
}

function kindOf(line: string): LineKind {
  if (line.startsWith("+++") || line.startsWith("---")) return "header"
  if (line.startsWith("@@")) return "hunk"
  if (line.startsWith("+")) return "add"
  if (line.startsWith("-")) return "del"
  return "context"
}

/** The unified diff exactly as the executor captured it; lines are coloured, never re-derived. */
export function UnifiedDiffView({ diff }: UnifiedDiffViewProps) {
  const { t } = useTranslation(NS)
  const lines = useMemo(() => {
    const split = diff.split("\n")
    if (split.length && split[split.length - 1] === "") split.pop()
    return split.map((text) => ({ text, kind: kindOf(text) }))
  }, [diff])
  const added = lines.filter((line) => line.kind === "add").length
  const removed = lines.filter((line) => line.kind === "del").length
  return (
    <div className="flex flex-col gap-1">
      <p className="text-n600 text-2xs">{t("diff.counts", { added, removed })}</p>
      <pre
        className="border-hair bg-surface max-h-[32rem] overflow-auto rounded-lg border py-2 font-mono text-xs"
        data-testid="trajectory-unified-diff"
      >
        {lines.map((line, index) => (
          <span
            key={index}
            data-line={line.kind}
            className={cn("block px-3 whitespace-pre-wrap", CLASSES[line.kind])}
          >
            {line.text || " "}
          </span>
        ))}
      </pre>
    </div>
  )
}

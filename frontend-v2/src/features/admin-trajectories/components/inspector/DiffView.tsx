import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { lineDiff } from "../../utils/diff"
import { TextBlock } from "./TextBlock"
import { NS } from "./types"

interface DiffViewProps {
  before: string
  after: string
}

const OP_CLASS = {
  same: "text-n700",
  add: "bg-diffadd text-ink",
  del: "bg-diffdel text-ink",
} as const

const OP_MARK = { same: " ", add: "+", del: "-" } as const

/** Line comparison of two captured values; too large to compare shows both in full instead. */
export function DiffView({ before, after }: DiffViewProps) {
  const { t } = useTranslation(NS)
  const lines = useMemo(() => lineDiff(before, after), [before, after])
  if (lines === null) {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-n500 text-xs">{t("diff.tooLarge")}</p>
        <TextBlock text={before} />
        <TextBlock text={after} />
      </div>
    )
  }
  const added = lines.filter((line) => line.op === "add").length
  const removed = lines.filter((line) => line.op === "del").length
  if (!added && !removed) return <p className="text-n500 text-xs">{t("diff.identical")}</p>
  return (
    <div className="flex flex-col gap-1">
      <p className="text-n600 text-2xs">{t("diff.counts", { added, removed })}</p>
      <pre
        className="border-hair bg-surface max-h-[32rem] overflow-auto rounded-lg border py-2 font-mono text-xs"
        data-testid="trajectory-diff"
      >
        {lines.map((line, index) => (
          <span key={index} className={cn("block px-3 whitespace-pre-wrap", OP_CLASS[line.op])}>
            {OP_MARK[line.op]} {line.text}
          </span>
        ))}
      </pre>
    </div>
  )
}

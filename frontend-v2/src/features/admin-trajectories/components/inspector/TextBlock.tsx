import { useState } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { CopyButton } from "./CopyButton"

interface Props {
  text: string
  mono?: boolean
  /** Long captured output renders a prefix first; the full value is one click away. */
  initialChars?: number
}

/** Verbatim text. Never interpreted: commands and markup in it are shown, not run. */
export function TextBlock({ text, mono = true, initialChars = 20_000 }: Props) {
  const { t } = useTranslation("admin-trajectories")
  const [expanded, setExpanded] = useState(false)
  const truncated = !expanded && text.length > initialChars
  return (
    <div className="border-hair bg-surface group relative rounded-lg border">
      <CopyButton
        text={text}
        label={t("common.copyText")}
        className="absolute end-1 top-1 opacity-70 group-hover:opacity-100"
      />
      <pre
        className={cn(
          "max-h-[32rem] overflow-auto p-3 pe-8 text-xs break-words whitespace-pre-wrap",
          mono ? "font-mono" : "font-sans",
        )}
      >
        {truncated ? text.slice(0, initialChars) : text}
      </pre>
      {truncated && (
        <button
          type="button"
          className="text-a700 px-3 pb-2 text-xs hover:underline"
          onClick={() => setExpanded(true)}
        >
          {t("common.showAll", { count: text.length })}
        </button>
      )}
    </div>
  )
}

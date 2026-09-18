// How full the context window is, as a ring beside the model picker.
//
// A ring rather than a token count: mid-conversation the useful question is
// "how close am I to compaction", which a fraction answers at a glance while
// "14.5k tokens" only answers if you happen to remember the model's window —
// and that window changes the moment the picker next to it changes. The exact
// numbers stay one hover away.
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { formatTokens } from "@/shared/lib/format"
import { Tooltip } from "@/shared/ui/Tooltip"

/** Geometry of the 16px ring: r=6 with a 2px stroke leaves a hairline of
 *  padding inside the box, so it sits level with the adjacent text. */
const R = 6
const CIRCUMFERENCE = 2 * Math.PI * R

interface Props {
  /** Tokens the next request will carry: history + system prompt + tools. */
  used: number
  /** The selected model's context window. */
  limit: number
  /** Effective auto-compaction ceiling from the server; absent when disabled/unknown. */
  compactionThreshold?: number
}

export function ContextRing({ used, limit, compactionThreshold }: Props) {
  const { t } = useTranslation("chat")
  // Nothing honest to draw until the backend has told us the window size.
  if (limit <= 0) return null

  const ratio = Math.min(1, Math.max(0, used / limit))
  // Never round a non-empty context down to a bare ring — 0% next to a visible
  // arc reads as a bug. Same at the top: only a genuinely full window says 100%.
  const pct = used > 0 ? Math.max(1, Math.round(ratio * 100)) : 0
  const threshold = compactionThreshold != null && Number.isFinite(compactionThreshold) && compactionThreshold > 0
    ? Math.min(limit, compactionThreshold) : undefined
  const pressure = threshold ? used / threshold : 0
  const level = ratio >= 1 || pressure >= 1 ? "critical" : pressure >= 0.875 ? "warn" : "calm"
  const stroke =
    level === "critical" ? "stroke-danger" : level === "warn" ? "stroke-accent" : "stroke-n700"
  const remaining = Math.max(0, limit - used)

  const detail = (
    <span className="flex flex-col gap-0.5">
      <span className="font-medium">{t("context.title")}</span>
      <span className="text-n700">
        {t("context.used", {
          used: formatTokens(used),
          limit: formatTokens(limit),
          pct,
        })}
      </span>
      <span className="text-n600">{t("context.left", { tokens: formatTokens(remaining) })}</span>
      {threshold != null && (
        <span className="text-n600">
          {t("context.compactAt", { tokens: formatTokens(threshold), pct: Math.round(threshold / limit * 100) })}
        </span>
      )}
      {threshold != null && level !== "calm" && (
        <span className={cn(level === "critical" ? "text-danger" : "text-accent")}>
          {t("context.compactSoon")}
        </span>
      )}
    </span>
  )

  return (
    <Tooltip label={detail}>
      <span
        className="flex size-8 flex-none items-center justify-center"
        role="img"
        aria-label={t("context.aria", { pct })}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
          <circle className="stroke-n400/70" cx="8" cy="8" r={R} fill="none" strokeWidth="2" />
          <circle
            className={cn(stroke, "transition-[stroke-dashoffset,stroke] duration-300")}
            cx="8"
            cy="8"
            r={R}
            fill="none"
            strokeWidth="2"
            strokeLinecap="round"
            strokeDasharray={CIRCUMFERENCE}
            strokeDashoffset={CIRCUMFERENCE * (1 - ratio)}
            // Start the arc at 12 o'clock and fill clockwise, the way every
            // other progress ring the user has met behaves.
            transform="rotate(-90 8 8)"
          />
        </svg>
      </span>
    </Tooltip>
  )
}

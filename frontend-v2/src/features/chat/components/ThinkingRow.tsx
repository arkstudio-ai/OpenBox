// What a turn shows before its first words arrive.
//
// This was three pulsing grey bars, which promise text that is about to appear.
// When a run stalls — an upstream account needing re-auth, five retries across
// a minute — those bars keep promising, and the wait reads as the app having
// hung rather than as work still in progress. Saying what is happening costs
// one line and removes the ambiguity.
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"

export function ThinkingRow({
  attempt,
  maxAttempts,
}: {
  /** Present only while retrying, so the wait is accounted for. */
  attempt?: number
  maxAttempts?: number
}) {
  const { t } = useTranslation("chat")
  const retrying = Boolean(attempt && attempt > 0)

  return (
    <div
      className="flex w-full items-center gap-2 pt-1"
      role="status"
      aria-live="polite"
    >
      <span className="flex flex-none items-center gap-1" aria-hidden>
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className={cn(
              "size-1.5 rounded-full",
              retrying ? "bg-sage" : "bg-n600",
              "animate-pulse-dot",
            )}
            // Staggered so the three read as one travelling pulse rather than
            // three things blinking together.
            style={{ animationDelay: `${i * 0.16}s` }}
          />
        ))}
      </span>
      <span className={cn("text-md", retrying ? "text-sage" : "text-n600")}>
        {retrying
          ? t("status.retrying", { attempt, total: maxAttempts ?? attempt })
          : t("status.thinking")}
      </span>
    </div>
  )
}

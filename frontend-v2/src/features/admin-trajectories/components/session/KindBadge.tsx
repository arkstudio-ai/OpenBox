import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { KIND_LABELS, KIND_LANES, LANE_CLASSES, labelKey } from "../../constants/labels"

interface KindBadgeProps {
  kind: string
  className?: string
}

/** Record type in words with its timeline lane colour, so a row and its bar read as the same thing. */
export function KindBadge({ kind, className }: KindBadgeProps) {
  const { t } = useTranslation("admin-trajectories")
  const lane = KIND_LANES[kind] ?? "state"
  return (
    <span
      className={cn(
        "text-n700 text-2xs inline-flex flex-none items-center gap-1.5 font-medium uppercase",
        className,
      )}
    >
      <span aria-hidden className={cn("size-2 flex-none rounded-full", LANE_CLASSES[lane])} />
      {t(labelKey(KIND_LABELS, kind, "kind.other"), { value: kind })}
    </span>
  )
}

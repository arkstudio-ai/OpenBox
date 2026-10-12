import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { rememberMode, type TrajectoryMode } from "../mode"

const LINK = "rounded-full px-3.5 py-1 text-xs transition-colors"

/** 简明 / 专业 — the two views of the same recordings; the URL decides which one is showing. */
export function ModeToggle({ mode }: { mode: TrajectoryMode }) {
  const { t } = useTranslation("admin-trajectories")
  const options: Array<{ value: TrajectoryMode; to: string }> = [
    { value: "lite", to: paths.adminTrajectories() },
    { value: "pro", to: paths.adminTrajectorySessions() },
  ]
  return (
    <div
      role="group"
      aria-label={t("lite.mode.label")}
      className="border-hair bg-card inline-flex flex-none items-center gap-0.5 rounded-full border p-0.5"
      data-testid="trajectory-mode-toggle"
    >
      {options.map((option) => (
        <Link
          key={option.value}
          to={option.to}
          aria-current={mode === option.value ? "page" : undefined}
          onClick={() => rememberMode(option.value)}
          className={cn(
            LINK,
            mode === option.value ? "bg-ink text-bg font-medium" : "text-n600 hover:bg-hairsoft",
          )}
        >
          {t(`lite.mode.${option.value}`)}
        </Link>
      ))}
    </div>
  )
}

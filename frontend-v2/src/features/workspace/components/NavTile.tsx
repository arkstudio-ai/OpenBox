import type { LucideIcon } from "lucide-react"
import { useMatch, useNavigate } from "react-router"
import { cn } from "@/shared/lib/cn"

interface NavTileProps {
  icon: LucideIcon
  label: string
  to: string
  /** Route pattern that counts as "here"; defaults to `to`. */
  pattern?: string
  /** One line on what the page holds, shown on hover. */
  hint?: string
  /** Unread count; hidden at zero, capped at 99+. */
  badge?: number
}

/** One centre page in the sidebar's tile grid: an icon over a short label that
 *  lights up while on it. On a short screen only the icon stays (the label is
 *  the tile's name and its tooltip), so the project list keeps its room. */
export function NavTile({ icon: Icon, label, to, pattern, hint, badge = 0 }: NavTileProps) {
  const navigate = useNavigate()
  const active = useMatch(pattern ?? to) !== null
  return (
    <button
      type="button"
      onClick={() => navigate(to)}
      aria-current={active ? "page" : undefined}
      aria-label={label}
      title={hint ? `${label} · ${hint}` : label}
      className={cn(
        "text-n800 hover:bg-hairsoft hover:text-ink relative flex h-13 min-w-0 flex-col items-center justify-center",
        "gap-1 rounded-xl px-0.5 [@media(max-height:760px)]:h-8.5",
        active && "bg-n200 text-ink font-medium",
      )}
    >
      <Icon size={17} strokeWidth={2.1} aria-hidden />
      <span className="w-full truncate text-center text-[11px] leading-3.5 [@media(max-height:760px)]:hidden">
        {label}
      </span>
      {badge > 0 && (
        <span
          data-testid={`nav-badge-${to}`}
          className="bg-accent text-bg absolute end-0.5 top-0.5 rounded-full px-1 text-[10px] leading-3.5 font-semibold"
        >
          {badge > 99 ? "99+" : badge}
        </span>
      )}
    </button>
  )
}

import type { LucideIcon } from "lucide-react"
import { useMatch, useNavigate } from "react-router"
import { cn } from "@/shared/lib/cn"

interface NavRowProps {
  icon: LucideIcon
  label: string
  to: string
  /** Route pattern that counts as "here"; defaults to `to`. Pass it when `to`
   *  carries a query string or the page has sub-routes. */
  pattern?: string
  className?: string
}

/** One sidebar row that leads to a centre page and lights up while on it. */
export function NavRow({ icon: Icon, label, to, pattern, className }: NavRowProps) {
  const navigate = useNavigate()
  const active = useMatch(pattern ?? to) !== null
  return (
    <button
      type="button"
      onClick={() => navigate(to)}
      aria-current={active ? "page" : undefined}
      className={cn(
        "text-ink hover:bg-hairsoft flex h-10 flex-none items-center gap-2.5 rounded-full px-1.5 text-base",
        active && "bg-n200 font-medium",
        className,
      )}
    >
      <span className="flex size-7 flex-none items-center justify-center">
        <Icon size={16} strokeWidth={2.1} />
      </span>
      {label}
    </button>
  )
}

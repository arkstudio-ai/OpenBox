import type { ReactNode } from "react"
import { cn } from "@/shared/lib/cn"

/** Shared shell for a chat task list and its team progress extension. */
export function TaskCardFrame({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("border-hair bg-card mb-2 flex w-full max-w-165 flex-col rounded-xl border p-4", className)}>{children}</div>
}

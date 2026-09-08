import type { ReactNode } from "react"
import { cn } from "@/shared/lib/cn"

interface Props {
  id?: string
  eyebrow: string
  title: string
  children: ReactNode
  className?: string
}

/** Landing section frame: anchor target, eyebrow + heading, then content. */
export function LandingSection({ id, eyebrow, title, children, className }: Props) {
  return (
    <section id={id} className={cn("mx-auto max-w-[1080px] scroll-mt-20 px-7 pt-22", className)}>
      <div className="flex flex-col gap-3">
        <Eyebrow>{eyebrow}</Eyebrow>
        <h2 className="max-w-[640px] text-pretty text-3xl leading-tight">{title}</h2>
      </div>
      {children}
    </section>
  )
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <span className="text-xs font-medium text-s700">{children}</span>
}

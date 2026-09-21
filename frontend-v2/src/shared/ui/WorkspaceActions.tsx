import { useContext, useMemo, useState, type ReactNode } from "react"
import { createPortal } from "react-dom"
import { WorkspaceActionsContext } from "./workspace-actions-context"

export function WorkspaceActionsProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<HTMLDivElement | null>(null)
  const value = useMemo(() => ({ target, setTarget }), [target])
  return <WorkspaceActionsContext value={value}>{children}</WorkspaceActionsContext>
}
export function WorkspaceActionsTarget() {
  const context = useContext(WorkspaceActionsContext)
  return <div ref={context?.setTarget} className="flex items-center gap-3 empty:hidden" />
}
export function WorkspaceActions({ children }: { children: ReactNode }) {
  const context = useContext(WorkspaceActionsContext)
  return context?.target ? createPortal(children, context.target) : null
}

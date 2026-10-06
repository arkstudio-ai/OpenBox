import { useCallback, useContext, useEffect, useRef, type ReactNode } from "react"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { AssistantReadContext } from "../hooks/assistant-read-context"
import { useAssistantReadCursor, type AssistantSnapshot } from "../api/assistant"

interface BoundaryProps { snapshot: AssistantSnapshot; children: ReactNode }
export function AssistantReadBoundary({ snapshot, children }: BoundaryProps) {
  const userId = useAuthStore((state) => state.user?.id)
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const scope = JSON.stringify([userId, workspaceId, snapshot.session?.id])
  const scopeMatches = !snapshot.session || snapshot.session.user_id === userId && snapshot.session.workspace_id === workspaceId
  return <ScopedAssistantReadBoundary key={scope} snapshot={snapshot} scopeMatches={scopeMatches}>{children}</ScopedAssistantReadBoundary>
}

function ScopedAssistantReadBoundary({ snapshot, scopeMatches, children }: BoundaryProps & { scopeMatches: boolean }) {
  const { mutate } = useAssistantReadCursor()
  const attempted = useRef(new Set<string>())
  const displayed = useCallback((messageId: string) => {
    const answer = snapshot.answers.find((item) => item.message_id === messageId)
    if (!scopeMatches || document.visibilityState !== "visible" || !answer?.available || !answer.display_token ||
      answer.sequence <= snapshot.last_seen_sequence || attempted.current.has(answer.display_token)) return
    attempted.current.add(answer.display_token)
    // A failed receipt waits for a fresh server display token, never a tight retry loop.
    if (attempted.current.size > 100) attempted.current = new Set([answer.display_token])
    mutate(answer)
  }, [snapshot, mutate, scopeMatches])
  return <AssistantReadContext.Provider value={{ snapshot, displayed }}>{children}</AssistantReadContext.Provider>
}

/** Observes actual final-answer content; opening the page or fetching a snapshot is not reading it. */
interface AnswerProps { messageId?: string | null; children: ReactNode }
export function VisibleAssistantAnswer({ messageId, children }: AnswerProps) {
  const context = useContext(AssistantReadContext)
  const ref = useRef<HTMLDivElement>(null)
  const answer = context?.snapshot.answers.find((item) => item.message_id === messageId)
  const displayed = context?.displayed
  useEffect(() => {
    const element = ref.current
    if (!element || !messageId || !answer?.available || !answer.display_token || !displayed) return
    const observer = new IntersectionObserver((entries) => {
      if (document.visibilityState === "visible" && entries.some((entry) =>
        entry.isIntersecting && entry.intersectionRect.height > 0 && entry.intersectionRect.width > 0)) {
        displayed(messageId)
      }
    })
    const observe = () => {
      observer.disconnect()
      // Re-observe on foregrounding to require a fresh visibility measurement.
      if (document.visibilityState === "visible") observer.observe(element)
    }
    observe()
    document.addEventListener("visibilitychange", observe)
    return () => { observer.disconnect(); document.removeEventListener("visibilitychange", observe) }
  }, [messageId, answer?.available, answer?.display_token, displayed])
  return <div ref={ref}>{children}</div>
}

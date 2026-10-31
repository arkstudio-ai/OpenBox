import { useState } from "react"
import type { Session } from "@/shared/types/api"

/** Stop for the composer: immediate in an ordinary conversation, a one-step
 *  inline confirmation in a task-linked one, where stop cancels the assistant's
 *  whole task and may leave it waiting for the owner to confirm outcomes. */
export function useStopWithConfirm(session: Session | undefined, busy: boolean, abortNow: () => void) {
  const stopsTask = session?.assistant_managed === true && session.kind !== "assistant"
  const [asked, setAsked] = useState(false)
  const confirm = () => { setAsked(false); abortNow() }
  const keep = () => setAsked(false)
  const stop = () => (stopsTask ? setAsked(true) : abortNow())
  // The question only makes sense while something is running; once the run
  // ends on its own the notice goes with it.
  return { stop, confirm, keep, stopsTask, showConfirm: asked && busy }
}

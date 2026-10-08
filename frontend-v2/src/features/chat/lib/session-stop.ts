import { makeClientId } from "./message"
import type { Session } from "@/shared/types/api"

type Target = NonNullable<Session["task_control"]>
type Stop = Pick<Target, "task_id" | "expected_revision" | "expected_run"> & { idempotency_key: string }
const held = new Map<string, Stop>()

/** An uncertain stop keeps its original target, even if polling finds a newer run. */
export function pendingTaskStop(scope: string, target: Target) {
  const key = `openbox:pending-task-stop:${scope}`
  let saved = held.get(key)
  if (!saved) {
    let raw: string | null = null
    try { raw = sessionStorage.getItem(key) } catch { /* In-memory replay remains available. */ }
    if (raw) {
      saved = JSON.parse(raw) as Stop
      if (!saved.task_id || !Number.isInteger(saved.expected_revision) || saved.expected_revision < 1
        || !saved.idempotency_key || !("expected_run" in saved)) throw new Error("Invalid pending task stop")
    }
  }
  const body = saved ?? { task_id: target.task_id, expected_revision: target.expected_revision,
    expected_run: target.expected_run ? { ...target.expected_run } : null, idempotency_key: makeClientId() }
  held.set(key, body)
  try { sessionStorage.setItem(key, JSON.stringify(body)) } catch { /* Keep the exact target in this tab. */ }
  return { body, confirmed: () => {
    if (held.get(key)?.idempotency_key === body.idempotency_key) held.delete(key)
    try {
      const raw = sessionStorage.getItem(key)
      if (raw && (JSON.parse(raw) as Stop).idempotency_key === body.idempotency_key) sessionStorage.removeItem(key)
    } catch { /* A newer or unreadable record must not be replaced. */ }
  } }
}

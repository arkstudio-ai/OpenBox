import { makeClientId } from "./message"
import type { SendMessageVars } from "../api/messages"

const pending = new Map<string, string>()
const PREFIX = "openbox:pending-send:"

function clearIdentity(storageKey: string, id: string) {
  if (pending.get(storageKey) === id) pending.delete(storageKey)
  try {
    if (sessionStorage.getItem(storageKey) === id) sessionStorage.removeItem(storageKey)
  } catch { /* Storage can be disabled. */ }
}

/** A materialized human message is a durable receipt even if HTTP was lost. */
export function confirmPendingSend(clientId: string) {
  const keys = new Set([...pending].filter(([, id]) => id === clientId).map(([key]) => key))
  try {
    for (let index = 0; index < sessionStorage.length; index++) {
      const key = sessionStorage.key(index)
      if (key?.startsWith(PREFIX) && sessionStorage.getItem(key) === clientId) keys.add(key)
    }
  } catch { /* Memory still covers this tab. */ }
  for (const key of keys) clearIdentity(key, clientId)
}

/** Persist only a body fingerprint and random key, never the message text.
 * A timeout followed by the same send (including after reload) keeps one key. */
export async function pendingSendIdentity(scope: string, vars: Omit<SendMessageVars, "clientMessageId">) {
  const body = JSON.stringify([scope, vars.text, vars.model, vars.variant, "variant" in vars,
    vars.videoModel, vars.videoResolution, vars.agent, vars.attachments ?? []])
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body))
  const digest = Array.from(new Uint8Array(bytes), (value) => value.toString(16).padStart(2, "0")).join("")
  const storageKey = `${PREFIX}${digest}`
  let id = pending.get(storageKey)
  try { id ??= sessionStorage.getItem(storageKey) ?? undefined } catch { /* Memory still covers this tab. */ }
  id ??= makeClientId()
  pending.set(storageKey, id)
  try { sessionStorage.setItem(storageKey, id) } catch { /* Storage can be disabled. */ }
  return { id, confirmed: () => clearIdentity(storageKey, id) }
}

import { useSyncExternalStore } from "react"

// One shared one-second clock for "still running" estimates while following
// live. Replay never uses it: a paused historical view reads recorded time.
let now = new Date().toISOString()
const listeners = new Set<() => void>()
let timer: number | null = null

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  if (timer === null) {
    now = new Date().toISOString()
    timer = window.setInterval(() => {
      now = new Date().toISOString()
      for (const notify of listeners) notify()
    }, 1_000)
  }
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0 && timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }
}

const noSubscribe = () => () => undefined
const current = () => now
const none = () => null

export function useNow(enabled: boolean): string | null {
  return useSyncExternalStore(enabled ? subscribe : noSubscribe, enabled ? current : none, none)
}

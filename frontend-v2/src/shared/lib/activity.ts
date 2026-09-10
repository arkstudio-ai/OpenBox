// A tiny "do not interrupt me" registry. Features mark themselves busy while
// something must not be cut short (a streaming turn); app-level housekeeping
// such as swapping to a new build asks before reloading the document.
const busy = new Set<string>()

export function setActivity(key: string, active: boolean): void {
  if (active) busy.add(key)
  else busy.delete(key)
}

export function isAppBusy(): boolean {
  return busy.size > 0
}

/** Test hook. */
export function resetActivity(): void {
  busy.clear()
}

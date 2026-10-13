import type { QueryClient } from "@tanstack/react-query"

/** Several surfaces share a store: own each event bridge once, until the last view closes. */
const subscriptions = new WeakMap<QueryClient, Map<string, { count: number; stop: () => void }>>()
export function retainChatSubscription(client: QueryClient, key: string, subscribe: () => () => void) {
  let entries = subscriptions.get(client)
  if (!entries) {
    entries = new Map()
    subscriptions.set(client, entries)
  }
  let subscription = entries.get(key)
  if (!subscription) {
    subscription = { count: 0, stop: subscribe() }
    entries.set(key, subscription)
  }
  subscription.count += 1
  return () => {
    subscription.count -= 1
    if (subscription.count === 0) {
      subscription.stop()
      entries.delete(key)
    }
  }
}

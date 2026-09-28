// WS → store bridge. `store.updated` fires when a store is created, patched,
// bound to a platform or its persona changes state; every store query
// (the store itself and its starter cards) refetches on it.
import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { wsClient } from "@/shared/ws/client"
import { storeKeys } from "../api/keys"
import { useUserId } from "../api/hooks"

export function useStoreEvents(): void {
  const qc = useQueryClient()
  const userId = useUserId()
  useEffect(() => {
    const invalidate = () => void qc.invalidateQueries({ queryKey: storeKeys.all(userId) })
    const offs = [wsClient.on("store.updated", invalidate), wsClient.on("__connected", invalidate)]
    return () => offs.forEach((off) => off())
  }, [qc, userId])
}

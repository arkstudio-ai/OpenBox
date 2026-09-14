import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { bindTrajectoryAccess } from "../api/access"

/**
 * Wires the purge triggers (sign-out, demotion, account switch, any 401/403 on
 * a trajectory request or socket) to this app's Query client. Mounted by the
 * trajectory routes; the binding intentionally outlives them so a sign-out on
 * any other page still clears what was loaded here.
 */
export function useTrajectoryAccessWatcher(): void {
  const client = useQueryClient()
  useEffect(() => {
    bindTrajectoryAccess(client)
  }, [client])
}

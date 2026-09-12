// The read-only watermark channel. It reuses the shared client's ticket and
// reconnect machinery with its own endpoints: the trajectory ticket keeps the
// access token's revocation identity and is not scoped to the viewer's current
// workspace, which the ordinary `/api/auth/ticket` would be.
import { WsClient } from "@/shared/ws/client"
import type { TrajectoryClientMessage, TrajectoryWsEventMap } from "@/shared/ws/events"

export const trajectorySocket = new WsClient<TrajectoryWsEventMap>({
  path: "/ws/admin/trajectories",
  ticketPath: "/api/admin/trajectories/ticket",
  // 403: no longer an admin. 404: admin reading is switched off. Neither is
  // fixed by retrying.
  terminalTicketStatuses: [403, 404],
  terminalCloseCodes: [4401, 4403],
})

/** Typed so a caller cannot put anything but a subscription frame on the wire. */
export function sendTrajectoryMessage(message: TrajectoryClientMessage): boolean {
  return trajectorySocket.send(message)
}

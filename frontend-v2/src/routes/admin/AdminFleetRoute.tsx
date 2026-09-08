import { FleetPage } from "@/features/admin"

/**
 * `/app/admin/fleet` predates the console and is still handed around in ops
 * runbooks, so it keeps its URL. The container, the heading and the role check
 * all moved up into AdminRoute / RequireAdmin — nothing is left here but the page.
 */
export default function AdminFleetRoute() {
  return <FleetPage />
}

import { useParams } from "react-router"
import { WorkspaceDetailPage } from "@/features/admin-billing"

/**
 * No tab bar here — the workspace detail is a leaf of the subscriptions tab and
 * renders its own back link. `:workspaceId` is a required segment, so the empty
 * fallback only satisfies the type; the page treats it as a miss either way.
 */
export default function AdminWorkspaceRoute() {
  const { workspaceId } = useParams()
  return <WorkspaceDetailPage workspaceId={workspaceId ?? ""} />
}

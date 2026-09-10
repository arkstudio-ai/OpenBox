import { NotificationsPage } from "@/features/admin-notifications"
import { useAuthStore } from "@/shared/api/auth-store"

export default function AdminNotificationsRoute() {
  const id = useAuthStore((s) => s.user?.id)
  return <NotificationsPage key={id} />
}

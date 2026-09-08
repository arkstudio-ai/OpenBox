// Username with the email underneath. Both are searchable server-side, so
// showing both is what makes a search result explicable.
import { DASH } from "@/features/admin-billing/lib/display"
import type { UserBrief } from "@/features/admin-billing/types"

export function UserCell({ user }: { user: UserBrief | null }) {
  // Payment history outlives the account that made it; the join comes back null.
  if (!user) return <span className="text-n500">{DASH}</span>
  return (
    <div className="flex min-w-0 flex-col">
      <span className="truncate text-ink">{user.username}</span>
      {user.email && <span className="truncate text-2xs text-n500">{user.email}</span>}
    </div>
  )
}

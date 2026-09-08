// Name over a muted id: operators recognise a workspace by name but paste the
// id into a ticket, so both have to be on the row.
import { DASH } from "@/features/admin-billing/lib/display"

export function WorkspaceCell({ name, id }: { name: string | null; id: string }) {
  return (
    <div className="flex min-w-0 flex-col">
      <span className="truncate text-ink">{name || DASH}</span>
      <span className="truncate font-mono text-2xs text-n500">{id}</span>
    </div>
  )
}

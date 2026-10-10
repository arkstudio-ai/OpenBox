// The cloud desktop as a page of its own: the sidebar's first centre row. In
// a chat the same view opens as a panel beside the conversation (takeover
// cards, the topbar toggle); here it gets the whole column.
import { DesktopTab } from "@/features/workbench"

export default function DesktopRoute() {
  return (
    <div className="flex min-h-0 flex-1 flex-col pt-1">
      <DesktopTab />
    </div>
  )
}

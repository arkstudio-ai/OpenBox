// Message centre (web): tabs per category with unread counts, an infinite
// feed, mark-all-read, and taps that mark the row read before routing.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"
import { toast } from "@/shared/ui/Toast"
import {
  INBOX_CATEGORIES,
  inboxKeys,
  markRead,
  readAll,
  useInboxFeed,
  useInboxUnread,
  type InboxCategory,
  type InboxItem,
} from "../api"
import { useOpenInboxLink } from "../lib/resolveLink"
import { InboxItemRow } from "./InboxItemRow"

const TABS: readonly { key: "all" | InboxCategory; category: InboxCategory | "" }[] = [
  { key: "all", category: "" },
  ...INBOX_CATEGORIES.map((category) => ({ key: category, category })),
]

const button = "min-h-9 rounded-full border border-hair px-4 text-sm hover:bg-hairsoft disabled:opacity-50"

export function InboxPage({ initialCategory = "" }: { initialCategory?: InboxCategory | "" }) {
  const { t } = useTranslation(["inbox", "common"])
  const qc = useQueryClient()
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const [tab, setTab] = useState<InboxCategory | "">(initialCategory)
  const unread = useInboxUnread()
  const feed = useInboxFeed(tab)
  const open = useOpenInboxLink()
  const workspaces = useWorkspaceStore((s) => s.items)
  const currentId = useWorkspaceStore((s) => s.currentId)
  const [busy, setBusy] = useState(false)

  const count = (category: InboxCategory | "") =>
    unread.data ? (category ? unread.data[category] : unread.data.total) : 0
  const label = (key: InboxCategory | "all", category: InboxCategory | "") => {
    const n = count(category)
    return n > 0 ? `${t(`tabs.${key}`)} ${n}` : t(`tabs.${key}`)
  }
  const invalidate = () => void qc.invalidateQueries({ queryKey: inboxKeys.all(userId) })

  const onOpen = async (item: InboxItem) => {
    let link = item.link
    if (item.readAt === null) {
      try {
        // The read receipt is also the authoritative copy of the link.
        link = (await markRead(item.id)).link
        invalidate()
      } catch {
        // Offline or already gone: still try the link we have.
      }
    }
    if ((await open(link)) === "unavailable") toast("warning", t("unavailable"))
  }

  const onReadAll = async () => {
    if (busy) return
    setBusy(true)
    try {
      await readAll(tab)
      invalidate()
    } catch {
      toast("error", t("loadFailed"))
    } finally {
      setBusy(false)
    }
  }

  const items = feed.data?.pages.flatMap((page) => page.items) ?? []
  const nameOf = (workspaceId: string | null) =>
    workspaceId && workspaceId !== currentId
      ? (workspaces.find((w) => w.id === workspaceId)?.name ?? workspaceId)
      : null

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <nav className="flex flex-wrap gap-1" aria-label={t("title")}>
          {TABS.map(({ key, category }) => (
            <button
              key={key}
              type="button"
              aria-current={tab === category ? "page" : undefined}
              onClick={() => setTab(category)}
              className={cn(
                "rounded-full px-4 py-2 text-sm",
                tab === category ? "bg-n300 text-ink font-medium" : "text-n700 hover:bg-n200",
              )}
            >
              {label(key, category)}
            </button>
          ))}
        </nav>
        <button className={button} onClick={() => void onReadAll()} disabled={busy || count(tab) === 0}>
          {t("readAll")}
        </button>
      </div>
      {feed.isPending && (
        <div className="flex justify-center py-16">
          <Spinner className="size-5" />
        </div>
      )}
      {feed.error && !feed.data && (
        <div className="flex flex-col items-center gap-2 py-16">
          <p className="text-n600 text-sm">{t("loadFailed")}</p>
          <button className={button} onClick={() => void feed.refetch()}>
            {t("action.retry", { ns: "common" })}
          </button>
        </div>
      )}
      {feed.data && items.length === 0 && <p className="text-n600 py-16 text-center text-sm">{t("empty")}</p>}
      {items.length > 0 && (
        <div className="border-hair bg-card divide-hairsoft flex flex-col divide-y rounded-2xl border p-1.5">
          {items.map((item) => (
            <InboxItemRow
              key={item.id}
              item={item}
              workspaceName={nameOf(item.workspaceId)}
              onOpen={(row) => void onOpen(row)}
            />
          ))}
        </div>
      )}
      {feed.hasNextPage && (
        <div className="flex justify-center">
          <button
            className={button}
            onClick={() => void feed.fetchNextPage()}
            disabled={feed.isFetchingNextPage}
          >
            {t("loadMore")}
          </button>
        </div>
      )}
    </div>
  )
}

// Message-centre transport (docs/MESSAGE_CENTER.md §用户接口). Query keys carry
// the user id (§7.2); `inbox.updated` on the socket invalidates the lot.
import { useEffect } from "react"
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "./http"
import { useAuthStore } from "./auth-store"
import { wsClient } from "../ws/client"

export type InboxCategory = "session" | "system" | "notice"
export const INBOX_CATEGORIES: readonly InboxCategory[] = ["session", "system", "notice"]

/** Allow-listed navigation targets; anything else opens the inbox itself. */
export type InboxLink =
  | { kind: "session"; workspaceId: string; sessionId: string; panel?: "desktop"; control?: boolean }
  | { kind: "cron"; workspaceId: string; jobId?: string }
  | { kind: "auth_center"; workspaceId: string; jobId?: string }
  | { kind: "skills"; workspaceId: string }
  | { kind: "admin_skills" }
  | { kind: "topic"; slug: string }
  | { kind: "url"; url: string }
  | { kind: "inbox" }
  | { kind: string }

export interface InboxItem {
  id: string
  category: InboxCategory
  kind: string
  title: string
  body: string
  link: InboxLink | null
  workspaceId: string | null
  announcementId: string | null
  readAt: string | null
  resolvedAt: string | null
  expiresAt: string | null
  createdAt: string
}

export interface InboxUnread {
  total: number
  session: number
  system: number
  notice: number
}

export interface InboxPage {
  items: InboxItem[]
  nextCursor: string | null
  unread: InboxUnread
}

export interface TopicPage {
  id: string
  slug: string
  title: string
  coverUrl: string | null
  contentMd: string
  ctaLabel: string | null
  ctaLink: InboxLink | null
  publishedAt: string | null
  updatedAt: string
}

export const inboxKeys = {
  all: (userId: string) => ["inbox", userId] as const,
  unread: (userId: string) => ["inbox", userId, "unread"] as const,
  feed: (userId: string, category: InboxCategory | "") => ["inbox", userId, "feed", category] as const,
  topic: (slug: string) => ["topic", slug] as const,
}

export function useInboxUnread() {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const enabled = useAuthStore((s) => s.isAuthenticated)
  return useQuery({
    queryKey: inboxKeys.unread(userId),
    enabled,
    queryFn: ({ signal }) => http.get<InboxUnread>("/api/inbox/unread", { signal }),
    refetchInterval: 120_000,
  })
}

export function useInboxFeed(category: InboxCategory | "") {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  return useInfiniteQuery({
    queryKey: inboxKeys.feed(userId, category),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const params = new URLSearchParams({ limit: "30" })
      if (category) params.set("category", category)
      if (pageParam) params.set("cursor", pageParam)
      return http.get<InboxPage>(`/api/inbox?${params}`, { signal })
    },
    getNextPageParam: (last) => last.nextCursor,
  })
}

/** Idempotent; the response is the row itself, link included. */
export const markRead = (id: string) => http.post<InboxItem>(`/api/inbox/${encodeURIComponent(id)}/read`)

export const readAll = (category: InboxCategory | "") =>
  http.post<{ updated: number }>(`/api/inbox/read-all${category ? `?category=${category}` : ""}`)

/** Public: no bearer needed, drafts are 404. */
export function useTopic(slug: string) {
  return useQuery({
    queryKey: inboxKeys.topic(slug),
    enabled: slug.length > 0,
    retry: false,
    queryFn: ({ signal }) => http.get<TopicPage>(`/api/topics/${encodeURIComponent(slug)}`, { signal }),
  })
}

/** Mount once under the workspace layout: badge and open feeds follow the socket. */
export function useInboxLiveEvents(): void {
  const qc = useQueryClient()
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  useEffect(() => {
    const invalidate = () => void qc.invalidateQueries({ queryKey: inboxKeys.all(userId) })
    const offs = [wsClient.on("inbox.updated", invalidate), wsClient.on("__connected", invalidate)]
    return () => offs.forEach((off) => off())
  }, [qc, userId])
}

// Transport for /api/admin/messages (docs/MESSAGE_CENTER.md §超管接口).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"

export type InboxLink =
  | { kind: "topic"; slug: string }
  | { kind: "url"; url: string }
  | {
      kind: "session" | "cron" | "auth_center" | "skills"
      workspaceId: string
      sessionId?: string
      jobId?: string
    }
  | { kind: "admin_skills" | "inbox" }

export type Audience =
  | { kind: "all" }
  | { kind: "users"; ids: string[] }
  | { kind: "workspace"; id: string }
  | { kind: "role"; role: "admin" | "user" }

export type AnnouncementStatus = "draft" | "scheduled" | "published" | "revoked"

export interface Announcement {
  id: string
  status: AnnouncementStatus
  title: string
  body: string
  link: InboxLink | null
  audience: Audience
  push: boolean
  publishAt: string | null
  expiresAt: string | null
  publishedAt: string | null
  fanoutAt: string | null
  fanoutCount: number
  createdBy: string
  createdAt: string
  updatedAt: string
  /** Present on single-item responses only. */
  recipientCount?: number
}

export interface AnnouncementInput {
  title: string
  body: string
  link: InboxLink | null
  audience: Audience
  push: boolean
  publishAt: string | null
  expiresAt: string | null
}

export interface Topic {
  id: string
  slug: string
  title: string
  coverUrl: string | null
  contentMd: string
  ctaLabel: string | null
  ctaLink: InboxLink | null
  status: "draft" | "published"
  createdBy: string
  createdAt: string
  updatedAt: string
  publishedAt: string | null
}

export interface TopicInput {
  slug: string
  title: string
  coverUrl: string | null
  contentMd: string
  ctaLabel: string | null
  ctaLink: InboxLink | null
}

const BASE = "/api/admin/messages"

export const keys = {
  announcements: ["admin-messages", "announcements"] as const,
  announcement: (id: string) => ["admin-messages", "announcement", id] as const,
  topics: ["admin-messages", "topics"] as const,
}

export function useAnnouncements() {
  return useQuery({
    queryKey: keys.announcements,
    queryFn: ({ signal }) =>
      http.get<{ items: Announcement[] }>(`${BASE}/announcements?limit=200`, { signal }),
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => item.status === "published" && !item.fanoutAt) ? 3000 : false,
  })
}

export function useAnnouncement(id: string | null) {
  return useQuery({
    queryKey: keys.announcement(id ?? ""),
    enabled: Boolean(id),
    queryFn: ({ signal }) =>
      http.get<Announcement>(`${BASE}/announcements/${encodeURIComponent(id!)}`, { signal }),
  })
}

export function useTopics() {
  return useQuery({
    queryKey: keys.topics,
    queryFn: ({ signal }) => http.get<{ items: Topic[] }>(`${BASE}/topics?limit=500`, { signal }),
  })
}

/** Every write invalidates both lists; a topic's slug may be what an announcement links to. */
function useInvalidate() {
  const client = useQueryClient()
  return () => client.invalidateQueries({ queryKey: ["admin-messages"] })
}

export function useAnnouncementWrites() {
  const invalidate = useInvalidate()
  const create = useMutation({
    mutationFn: (body: AnnouncementInput) => http.post<Announcement>(`${BASE}/announcements`, body),
    onSuccess: invalidate,
  })
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: AnnouncementInput }) =>
      http.put<Announcement>(`${BASE}/announcements/${encodeURIComponent(id)}`, body),
    onSuccess: invalidate,
  })
  const publish = useMutation({
    mutationFn: (id: string) =>
      http.post<Announcement>(`${BASE}/announcements/${encodeURIComponent(id)}/publish`),
    onSuccess: invalidate,
  })
  const revoke = useMutation({
    mutationFn: (id: string) =>
      http.post<Announcement & { hidden: number }>(`${BASE}/announcements/${encodeURIComponent(id)}/revoke`),
    onSuccess: invalidate,
  })
  const preview = useMutation({
    mutationFn: (id: string) =>
      http.post<{ id: string }>(`${BASE}/announcements/${encodeURIComponent(id)}/preview`),
  })
  return { create, update, publish, revoke, preview }
}

export function useTopicWrites() {
  const invalidate = useInvalidate()
  const create = useMutation({
    mutationFn: (body: TopicInput) => http.post<Topic>(`${BASE}/topics`, body),
    onSuccess: invalidate,
  })
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: TopicInput }) =>
      http.put<Topic>(`${BASE}/topics/${encodeURIComponent(id)}`, body),
    onSuccess: invalidate,
  })
  const publish = useMutation({
    mutationFn: (id: string) => http.post<Topic>(`${BASE}/topics/${encodeURIComponent(id)}/publish`),
    onSuccess: invalidate,
  })
  const unpublish = useMutation({
    mutationFn: (id: string) => http.post<Topic>(`${BASE}/topics/${encodeURIComponent(id)}/unpublish`),
    onSuccess: invalidate,
  })
  return { create, update, publish, unpublish }
}

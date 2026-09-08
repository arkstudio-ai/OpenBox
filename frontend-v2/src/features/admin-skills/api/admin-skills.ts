// Admin skill-store data hooks. Components never fetch directly (§7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http, requestBlob } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { buildQuery, type QueryValue } from "@/features/admin-skills/lib/query"
import type {
  InstallRecord,
  Page,
  ReviewDetail,
  SkillListing,
  StoreEntry,
} from "@/features/admin-skills/types"
import { adminSkillKeys } from "./keys"

const BASE = "/api/admin/skills"

/** `catalog_id` always contains a colon, so it must be encoded into a path. */
function entryPath(suffix: string, catalogId: string): string {
  return `${BASE}/${suffix}/${encodeURIComponent(catalogId)}`
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anon")
}

export function useStoreEntries(params: Record<string, QueryValue>) {
  const userId = useUserId()
  const query = buildQuery(params)
  return useQuery({
    queryKey: adminSkillKeys.store(userId, query),
    queryFn: () => http.get<Page<StoreEntry>>(`${BASE}/store${query}`),
    // Keeps the previous page on screen while the next one loads, so paging
    // does not flash the empty state between two full pages.
    placeholderData: (previous) => previous,
  })
}

export function useReviewQueue(params: Record<string, QueryValue>) {
  const userId = useUserId()
  const query = buildQuery(params)
  return useQuery({
    queryKey: adminSkillKeys.review(userId, query),
    queryFn: () => http.get<Page<StoreEntry>>(`${BASE}/review${query}`),
    placeholderData: (previous) => previous,
  })
}

export function useReviewDetail(catalogId: string | null) {
  const userId = useUserId()
  return useQuery({
    queryKey: adminSkillKeys.reviewDetail(userId, catalogId ?? ""),
    queryFn: () => http.get<ReviewDetail>(entryPath("review", catalogId ?? "")),
    enabled: !!catalogId,
  })
}

export function useInstalls(params: Record<string, QueryValue>) {
  const userId = useUserId()
  const query = buildQuery(params)
  return useQuery({
    queryKey: adminSkillKeys.installs(userId, query),
    queryFn: () => http.get<Page<InstallRecord>>(`${BASE}/installs${query}`),
    placeholderData: (previous) => previous,
  })
}

/**
 * Every write here changes what ordinary users see in the skill centre, so it
 * invalidates that cache too — otherwise an approved skill stays invisible in
 * the store tab until a reload.
 */
function useAdminSkillMutation<T>(mutationFn: (vars: T) => Promise<unknown>) {
  const client = useQueryClient()
  const userId = useUserId()
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminSkillKeys.root })
      void client.invalidateQueries({ queryKey: adminSkillKeys.skillCenter(userId) })
    },
  })
}

export interface ListingVars {
  catalogId: string
  listing: Extract<SkillListing, "listed" | "delisted">
  /** Required by the server when delisting; it reaches the author as a notice. */
  note?: string
}

export function useSetListing() {
  return useAdminSkillMutation((vars: ListingVars) =>
    http.post(entryPath("store", vars.catalogId) + "/listing", {
      listing: vars.listing,
      note: vars.note?.trim() || undefined,
    }),
  )
}

export function useSetFeatured() {
  return useAdminSkillMutation((vars: { catalogId: string; featured: boolean }) =>
    http.post(entryPath("store", vars.catalogId) + "/featured", { featured: vars.featured }),
  )
}

export function useSetOfficial() {
  return useAdminSkillMutation((vars: { catalogId: string; isOfficial: boolean }) =>
    http.post(entryPath("store", vars.catalogId) + "/official", { is_official: vars.isOfficial }),
  )
}

export function useApproveSubmission() {
  return useAdminSkillMutation((vars: { catalogId: string; note?: string }) =>
    http.post(entryPath("review", vars.catalogId) + "/approve", {
      note: vars.note?.trim() || undefined,
    }),
  )
}

export function useRejectSubmission() {
  return useAdminSkillMutation((vars: { catalogId: string; note: string }) =>
    http.post(entryPath("review", vars.catalogId) + "/reject", { note: vars.note.trim() }),
  )
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  anchor.rel = "noopener"
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** Reviewers read the submitted ZIP locally; the download itself is audited. */
export function useDownloadArchive() {
  return useMutation({
    mutationFn: async (vars: { catalogId: string; name: string }) => {
      const result = await requestBlob(entryPath("review", vars.catalogId) + "/archive")
      saveBlob(result.blob, result.filename || `${vars.name}.zip`)
    },
  })
}
